# AI News Digest Engine — Overview

A self-hosted modular system that produces a daily AI-curated news digest (5
main stories + 5 headlines) from RSS feeds, using a local LLM (`llama.cpp`) and
delivering via **Telegram** / **Email**.

---

## Core Principle: Hub and Spoke

| Role | Description | Technology |
|------|-------------|------------|
| **Hub (Temporal)** | Orchestration, scheduling, retries, state | Temporal.io (Docker, self-hosted) |
| **Spokes (FastAPI + Temporal activities)** | Each activity owns **ONE responsibility** | FastAPI + Python 3.11+ |

The hub is **Temporal**: a durable workflow (`DigestPipelineWorkflow`)
choreographs the pipeline, and Temporal Schedules trigger it daily. The spokes
are the FastAPI microservices plus the Temporal activities in `src/workers/`.

---

## Components

| Component | Role | Technology |
|-----------|------|------------|
| **temporal** | Orchestration, scheduling, retries | Temporal.io server (`temporalio/auto-setup`) |
| **temporal-ui** | Web UI for workflows / schedules | `temporalio/ui` |
| **worker** | Runs the digest workflow + activities | Python `temporalio.worker.Worker` |
| **narration-worker** | Runs the narration workflow + activities | Python `temporalio.worker.Worker` |
| **api** | Business logic via REST endpoints | FastAPI + Python 3.11+ |
| **llm** | Local inference server | `llama.cpp` (`/v1/chat/completions`) via LiteLLM |
| **db** | Delivery history, dedup state | SQLite (`news.db`) |
| **storage** | Generated audio, cached articles | Local filesystem (`./data/`) |

---

## Data Flow (Full Pipeline)

```
[Temporal Schedules — one per collection]   (collections.toml → HH:MM UTC each)
    |
    v
[DigestPipelineWorkflow.run]                  (src/workers/workflows.py)
    |                                         config.collection scopes everything
    v
[fetch_rss_activity]            --> [feedparser → articles]
    |
    v
[filter_new_activity]           --> [SQLite: (collection, url) unseen in 7-day window?]
    |
    v
[cluster_activity]              --> [group by event fingerprint, collection-scoped]
    |
    v
[generate_digest_activity]      --> [llama.cpp → 5+5 markdown, per-collection template]
    |
    v
[generate_tts_activity] (opt.)  --> [engine lease → tts_backends → .mp3/.wav]
    |
    v
[deliver_telegram_*_activity]   --> [text + audio]  (per-collection chat_id optional)
```

Each arrow is a `workflow.execute_activity` call with a start-to-close timeout;
the workflow is fully deterministic, so Temporal can resume it after any crash.
Article dedup, event clusters, and delivery history are all keyed by collection
in the shared SQLite database, so the same URL may appear in several digests.

### TTS engine routing (Phase 8)

The digest markdown becomes plain text (inline emotion tags such as `[happy]`
kept), truncated to `TTS_MAX_CHARS`, and is handed to **the engine the collection
asked for** (`tts_backend`, resolved outside the workflow by
`pipeline_config_for`):

| backend | call | output |
|---------|------|--------|
| `edge` (default) | client-side `edge_tts.Communicate` | `.mp3` |
| `omni_voice` | `POST {TTS_API_BASE_URL}/tts` (the `tts-api` gateway) | `.wav` |
| `qwen3_tts` | local gateway `engine=qwen3_tts` (default) or `modal_client.modal_tts()` when `run_in_modal` | `.wav` |
| `zonos2` | `modal_client.modal_zonos2_tts()` | `.wav` |

`qwen3_tts` runs locally on the gateway unless `run_in_modal` is set (Phase 10).
The local engine has **no stock voices**, so a catalog profile name (committed
or operator-designed) is registered once as a gateway clone pinned to
`qwen3_tts` (`src/api/services/gateway_voices.py`), reusing the reference WAV
and its `ref_text`; the assigned clone UUID then drives synthesis. Gateway-owned
ids (stock ids and clone UUIDs) pass through untouched.

`src/api/services/tts_backends.py` owns the dispatch, the per-engine voice
resolution and the output extension. The engine is part of the cache hash
(`SIDECAR_VERSION = 2`), so switching engines regenerates instead of serving
another engine's bytes, and a `TTS_FALLBACK_TO_EDGE` degradation is written
**without** a sidecar so the next run retries the requested engine. Everything
downstream (M4A transcode, Whisper verification, Telegram) is ffmpeg-based and
extension-driven.

### The engine lease (Phase 8.1)

The digest worker and the doc-tts worker are separate containers sharing one
`data/` volume, so synthesis is serialized by a **priority lease in SQLite**
(migration `011_tts_lease.sql`, service `src/api/services/tts_lease.py`):

```
generate_tts_activity (priority 10)          DocumentTtsWorkflow (priority 0)
-------------------                          --------------------------------
INSERT waiter row  ────────────────────┐     holds the lease for the whole
queue for the lease                   │     render, renewing every ttl/3
                                      └──►  before each chunk: waiter_above(0)?
acquire → synthesize → release                    yes → release, status=PAUSED,
                                                  wait for the digest to finish,
                                                  re-acquire, status=GENERATING
```

The waiter row written *before* the acquire is the yield signal, which is what
makes the hand-over non-overlapping: no clock, no timezone, and a paused or
disabled collection never pauses a book. A live lease is never stolen; the TTL
(300 s, renewed while a synthesis runs) is only the safety net for a holder that
died. `TTS_LEASE_ENABLED=false` turns the whole protocol off.

`GET /api/admin/documents/tts/{id}` reports `estimate_wall_seconds` (the Phase
8.5 forecast, written by the extraction step) and `eta_seconds` (extrapolated
from the measured rate of the chunks already finished), plus `paused`.

### Voice registry, engine guard and custom voices (Phase 9)

Each engine owns a different voice vocabulary (Edge ShortNames, gateway stock
ids and clone UUIDs, Modal catalog profile names), so
`src/api/services/voice_registry.py` merges every source into one
`VoiceOption{id, label, language, engines, category}` list — `category` is
`stock | cloned | generated` and a source that cannot be reached degrades to
`[]` instead of breaking the page. `GET /api/admin/voices/all` exposes the
merge, and every GUI selector that is not engine-bound is fed from it: the
tools pickers (searchable radios, Phase 9.7) and the unified `/ui/voices`
table (Phase 9.6). The collection digest voice and the cast stay
**per-engine** selects (`_tts_voice_options`), because an engine only accepts
its own ids: the cast refreshes each row's options out-of-band via
`GET /ui/fragments/cast_voices` (`hx-swap-oob`) when the backend changes, and
document TTS stays Edge-only (user decision).

Both search and table share one service, `src/api/services/voice_search.py`:
a glob-style query (`*`/`?`, literals escaped, case-folded and **unanchored**
— `Mich*clo` finds "Michael_sss en(glish) Voz clonada"` — plain text is a
substring), a language prefix filter (`es` matches `es` and `es-ES`), an
exact category/engine filter, a display order (generated → cloned → stock)
and page slicing (25/page, clamp to valid range). The tools voice pickers
(`partials/_voice_picker.html` + `_voice_picker_results.html`,
`GET /ui/fragments/voice_picker`) render up to 20 radio results with a total
count; the selection always survives searches (the checked radio travels with
the htmx include and the fragment keeps it, checked, even when it falls
outside the match), and a query with zero matches adds the literal "Usar «q»
tal cual" option so an unknown id can be sent as-is. The lesson pickers'
results container also listens for changes and re-runs
`GET /ui/fragments/lesson_voices` so the mixed-engine submit guard recomputes
after every pick. The unified table (`partials/_voices_table.html`,
`GET /ui/fragments/voices_table`) filters with the same semantics, joins the
design status (`voice_catalog_admin.generated_voices()`) for the badge and
WAV link, and paginates with url-encoded filter state carried by the pager.

`src/api/services/voice_engine.py` classifies a voice id with no network:
Edge/gateway patterns and catalog names match, anything else is **unknown =
wildcard** (lenient by decision, so legacy ids keep round-tripping).
`assert_voice_engine` / `assert_same_engine` run in `CollectionIn`,
`NarrationSpec`, `TTSIn` and the GUI forms, and `resolve_cast` infers a
lesson's `tts_backend` from its voices, rejecting mixed-engine selections
before anything is queued.

Custom voices (menu `/ui/voices`): `POST /api/admin/voices/custom` (or the
equivalent UI form) persists the profile in
`data/voices/custom_profiles.json`, which `load_profiles()` merges with the
committed catalog (name collisions are refused, writes are atomic), then
starts `VoiceCatalogWorkflow` in `news-engine-voices`. A profile counts as
*generated* as soon as `data/voices/refs/<name>.wav` exists
(`voice_catalog_admin.generated_status` → `pendiente | generada |
desactualizada`). `voice_preview.preview()` speaks a fixed phrase through the
voice's preferred engine, caches the sample in `data/voices/samples/` and
falls back to the local reference WAV while `MODAL_ENABLED` is off.

### Narration branch (Phase 7, in progress)

The narration pipeline is a second, independent audio spoke that hangs off the
same digest. It is generic multi-speaker narration, not a news-only podcast.

Phase 7.9b splits podcast generation into one namespace per backend. A
Temporal activity always executes in the namespace of the workflow that
scheduled it, so the digest cannot call the render activities directly any
more: it calls a single dispatch activity that starts and awaits
`NarrationWorkflow` in the podcast namespace that owns the backend.

```
[generate_tts_activity]  -->  [digest_runs.audio_path]        (single voice, Edge)
     |
     v  (PipelineConfig.narration, filled outside the workflow: per-collection
     |   opt-in + narration_enabled kill switch + the podcast namespace and
     |   task queue)
[record_run_activity]   -->  [digest_runs]                   (row must exist first)
     |
     v  in the digest namespace, on the digest queue
[dispatch_narration_activity]   (src/workers/activities.py: starts the workflow
     |                           below and awaits it, heartbeating while it renders)
     |   workflow id = narration-<collection>-<run_id>, so a retry reattaches to the
     |   render already in flight instead of paying for a second one
     v  cross-namespace: news-engine-podcast-local | news-engine-podcast-modal
[narration-worker | narration-modal-worker]
     |                  (src/workers/run_narration_worker.py | run_modal_worker.py:
     |                   same image, own namespace and queue, only NarrationWorkflow
     |                   + narration activities)
     |--> [resolve_narration_script_activity]  inline script or narration_scripts row
     |--> [render_narration_activity]  --> TTS gateway /tts | /conversations
     |                                    (conversations: one request per block,
     |                                     blocks joined with the turn pause)
     |                                    + ffmpeg music bed + M4A encode
     v
[digest_runs.narration_path]  +  audio_jobs row with kind='narration'
```

The audio comes back to the digest through the dispatch activity's result, so
the delivery of that audio still happens in `DigestPipelineWorkflow`; a
narration failure is recorded in `delivery_status` and never fails the digest.
`src/workers/podcast_target.py` is the one place that maps a spec's backend to
its namespace and queue, shared by the schedule, the API trigger and the CLI
trigger so all three agree.

`NarrationWorkflow` (`src/workers/narration_workflows.py`) is the same pair of
activities as a standalone, first-class workflow, started manually with
`python -m src.workers.trigger_narration`, so a cast can be rendered and
debugged without waiting for a scheduled digest.

Both audio paths coexist on the same run: `audio_jobs` is keyed by
`(run_id, kind)`, `digest_runs.audio_path` stays reserved for the digest TTS and
`narration_path` for the narration, and narration files live in
`data/narration/` so their `.meta.json` sidecars cannot collide with
`data/tts_cache/`. See Phases.md → Phase 7 and CONVENTIONS.md → Narration.

### Narration delivery (Phase 7.6)

When a collection has `narration_enabled = true` and `narration_deliver_audio = true`
(default), the generated narration M4A is delivered to the collection's
`delivery_channels` (Telegram audio, email attachment, WhatsApp audio). This is in
addition to the text digest and the optional TTS audio.

If the narration render fails, an error notification is sent to the same
channels. For **dialog/podcast renders** (two or more distinct voices, or the
`conversations` strategy), the fallback is controlled by
`NARRATIONS_DIALOG_FALLBACK_TO_EDGE` (default `false`), which overrides the
general `NARRATION_FALLBACK_TO_EDGE` for dialog renders. This ensures cloned
voices (e.g. Dwight/MichaelScott) are never silently substituted — the render
fails and the error alert fires instead.

---

## Key Decisions

| Decision | Rationale |
|----------|-----------|
| **Temporal over n8n / cron** | Durable execution, built-in retries, schedules as code, dev-friendly tests |
| **FastAPI over monolithic script** | Each phase = one endpoint, independently testable |
| **Activities call services directly** | No HTTP hop between workflow steps; single deployable |
| **SQLite over Postgres** | Single file, zero ops, sufficient for personal use (Temporal keeps its own Postgres) |
| **llama.cpp over cloud LLMs** | Privacy, zero cost, local control |
| **LiteLLM as LLM adapter** | Swap models without changing business code |
| **Event fingerprinting over pure embeddings** | Faster, deterministic, debuggable first pass |
| **No zigflow; Temporal Flow as optional desktop viewer** | zigflow's YAML DSL compiles to its own Go worker and would replace the tested Python SDK workflows; Temporal Flow is a read-mostly overlay that needs no code/container changes (see Observability) |
| **One voice registry + lenient multi-engine guard (Phase 9)** | Every dropdown lists all engines' stock/cloned/generated voices; unknown ids stay wildcards so legacy values never break, and only a mixed-engine selection is rejected |
| **Role map in one module + session cookies (Phase 6 RBAC)** | `src/api/authz.py` is the only place that decides who may do what (`required_role`/`has_role`; unlisted writes default to admin, so a new endpoint is never accidentally open); logins mint an opaque 12 h session token stored server-side instead of putting credentials in the browser, and non-admin users hold no password at all — each login emails a 6-digit single-use code |

---

## Observability

The built-in **Temporal Web UI** (docker-compose `temporal-ui`, `http://localhost:8081`) is the
primary debugging surface: worklist, Compact view, and the Timeline view render each workflow's
event history (activities, timers, retries) without extra tooling.

**System monitoring (PHASE 5.3):** the `SystemMonitorWorkflow` (schedule `system-monitor`,
`MONITOR_INTERVAL_MINUTES`) runs on the worker at fixed intervals and reports the health of the
whole stack:

- Every check is an async function in `src/api/services/monitoring/checks.py` registered in
  `MONITOR_CHECKS`; adding a monitor = one function + one registry row (no workflow/activity
  changes). Checks cover Docker containers (read-only UNIX socket, section below), the edge-tts
  backend, the TTS server disk endpoint, the internal services (api / Temporal / whatsapp
  gateway), `collections.toml` + its feeds + Temporal schedules, the latest digest runs per
  collection, and the host disc (plus `data/tts_cache/` size).
- Checks run inside **one activity** (`run_monitor_checks_activity`) so all I/O stays out of the
  deterministic workflow. Outcomes land in both SQLite (`monitor_events`, retention
  `MONITOR_RETENTION_DAYS`) and daily-rotated JSONL logs (`data/logs/monitor_YYYY-MM-DD.jsonl`).
- Alerts are **transition-based**: a check alerts only when it moves to `error`/`warning`
  (previous status stored in the KV store), so a persistent problem does not spam. A once-per-UTC-
  day full summary and per-done-run notifications are optional (`MONITOR_DAILY_SUMMARY`,
  `MONITOR_ALERT_ON_RUN`). Delivery channels are `MONITOR_NOTIFY_CHANNELS` (telegram, email,
  whatsapp).
- Reading Docker state uses the Engine REST API over the **Docker UNIX socket** mounted
  read-only on the worker (`/var/run/docker.sock:/var/run/docker.sock:ro`); no docker-in-docker,
  no new HTTP endpoint.
- `CollectionsMonitorWorkflow` computes a diff (`collections_changes_activity`) before each
  schedule resync and alerts when a collection is added or removed.

**Temporal Flow** (`github.com/itaisoudry/temporal-flow-web`, community-contributed, MIT server
component / proprietary desktop app) is an optional interactive graph overlay for exploring parent
and child workflows. It is **not** a container or a docker-compose overlay: it is a desktop
(Electron) application that connects to the self-hosted Temporal UI address (e.g.
`localhost:8081`) and discovers namespaces automatically. It requires no code changes and no new
service. To use it from a dev machine other than the server, expose `:8081` (or use an SSH tunnel).

Thoroughly evaluated but deliberately **not** adopted: **zigflow** (CNCF Open Workflow Spec DSL
that compiles to Temporal workflows with its own Go worker). It would require abandoning the Python
SDK workflows, `WorkflowEnvironment` tests, and the non-blocking delivery logic in
`DigestPipelineWorkflow`, with no benefit for a single-workflow pipeline.

---

## Project Structure

```
news-engine/
├── Architecture.md
├── CONVENTIONS.md
├── Phases.md
├── Deploy.md
├── AGENTS.md
├── pyproject.toml
├── docker-compose.yml          # api + worker + temporal + temporal-ui + postgres
├── collections.toml            # collections config (copy of collections.toml.example)
├── .env.example
├── src/
│   ├── api/
│   │   ├── main.py             # FastAPI app
│   │   ├── auth.py             # session/role-guard middleware (Phase 6: cookie
│   │   │                       # sessions, 401/403, identity on request.state)
│   │   ├── authz.py            # role map: required_role/has_role (admin >
│   │   │                       # user > viewer) + PUBLIC_ROUTES, single truth
│   │   ├── routes_ui.py        # Jinja2 + HTMX admin GUI pages
│   │   ├── templates/          # GUI templates (base + pages + HTMX partials)
│   │   ├── static/             # vendored htmx.min.js
│   │   ├── routes/             # /health, /mvp_digest, /filter_new, /cluster,
│   │   │                       # /summarize, /admin/* (collections, voices,
│   │   │                       # runs, schedules, tts). /generate_tts is a
│   │   │                       # 410 Gone alias kept for one release.
│   │   ├── services/           # Business logic (called by routes AND activities)
│   │   │                       # incl. collections_repo, voices, temporal_admin,
│   │   │                       # login_codes (Phase 6 one-time email codes)
│   │   ├── models/             # Pydantic schemas (incl. narration, Phase 7)
│   │   └── db/                 # SQLite layer + migrations
│   ├── core/
│   │   ├── config.py           # pydantic-settings (tts_* + narration_*)
│   │   ├── collections.py      # collections.toml loader (CollectionConfig)
│   │   ├── prompts.py          # LLM prompt templates + DIGEST_TEMPLATES registry
│   │   └── llm.py              # LiteLLM wrapper
│   └── workers/
│       ├── workflows.py        # DigestPipelineWorkflow + PipelineConfig
│       ├── activities.py       # RSS, filter, cluster, digest, TTS, Telegram,
│       │                       # record_run (run history)
│       ├── run_worker.py       # digest worker entry point
│       ├── narration_workflows.py  # NarrationWorkflow + NarrationConfig
│       ├── narration_activities.py # resolve script + render narration
│       ├── run_narration_worker.py # narration worker (own task queue)
│       ├── trigger.py          # manual one-shot run (--collection)
│       ├── trigger_narration.py # manual one-shot narration render
│       └── schedule.py         # one Temporal schedule per collection
├── data/                       # SQLite DB + tts_cache/ + narration/ (gitignored)
├── tests/
│   ├── unit/
│   └── integration/
└── scripts/
    ├── auto_commit.sh
    └── deploy.sh
```

---

## Non-Goals (v1)

- [x] Multi-user / per-user accounts → **done in Phase 6 (RBAC)**:
      self-registration + admin approval, OTP login by email, roles
      admin / user / viewer enforced by the middleware
- [ ] Vector database (Qdrant / Milvus)
- [ ] Web administration UI → **done in Phase 6** (FastAPI + Jinja2 + HTMX)
- [ ] Real-time streaming