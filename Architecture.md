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
| **worker** | Runs the workflow + activities | Python `temporalio.worker.Worker` |
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
[generate_tts_activity] (opt.)  --> [OmniVoice API → fragmentado → .wav]
    |
    v
[deliver_telegram_*_activity]   --> [text + audio]  (per-collection chat_id optional)
```

Each arrow is a `workflow.execute_activity` call with a start-to-close timeout;
the workflow is fully deterministic, so Temporal can resume it after any crash.
Article dedup, event clusters, and delivery history are all keyed by collection
in the shared SQLite database, so the same URL may appear in several digests.

The TTS step converts the digest markdown to plain text (keeping inline OmniVoice
emotion tags such as `[happy]`), fragments it into small paragraphs (default
400 chars) to avoid long-input hallucinations, synthesizes each chunk via the
OmniVoice `POST /api/v1/tts` endpoint, and concatenates the resulting WAV files.

### Narration branch (Phase 7, in progress)

The narration pipeline is a second, independent audio spoke that hangs off the
same digest. It is generic multi-speaker narration, not a news-only podcast.

```
[generate_tts_activity]  -->  [digest_runs.audio_path]        (single voice, Edge)
     |
     v  (PipelineConfig.narration, filled outside the workflow: per-collection
     |   opt-in + narration_enabled kill switch + the narration task queue)
[record_run_activity]   -->  [digest_runs]                   (row must exist first)
     |
     v  dispatched by NAME with ActivityOptions(task_queue=<narration_task_queue>)
[render_narration_activity]   (src/workers/narration_activities.py)
     |
     v
[narration-worker]      (src/workers/run_narration_worker.py: same image, own
     |                  queue, only NarrationWorkflow + narration activities)
     |--> [resolve_narration_script_activity]  inline script or narration_scripts row
     |--> [render_narration_activity]  --> TTS gateway /tts | /conversations
     |                                    + ffmpeg music bed + M4A encode
     v
[digest_runs.narration_path]  +  audio_jobs row with kind='narration'
```

`NarrationWorkflow` (`src/workers/narration_workflows.py`) is the same pair of
activities as a standalone, first-class workflow, started manually with
`python -m src.workers.trigger_narration`, so a cast can be rendered and
debugged without waiting for a scheduled digest.

Both audio paths coexist on the same run: `audio_jobs` is keyed by
`(run_id, kind)`, `digest_runs.audio_path` stays reserved for the digest TTS and
`narration_path` for the narration, and narration files live in
`data/narration/` so their `.meta.json` sidecars cannot collide with
`data/tts_cache/`. See Phases.md → Phase 7 and CONVENTIONS.md → Narration.

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

---

## Observability

The built-in **Temporal Web UI** (docker-compose `temporal-ui`, `http://localhost:8080`) is the
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
`localhost:8080`) and discovers namespaces automatically. It requires no code changes and no new
service. To use it from a dev machine other than the server, expose `:8080` (or use an SSH tunnel).

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
│   │   ├── auth.py             # ADMIN_API_TOKEN middleware for /ui + /api/admin
│   │   ├── routes_ui.py        # Jinja2 + HTMX admin GUI pages
│   │   ├── templates/          # GUI templates (base + pages + HTMX partials)
│   │   ├── static/             # vendored htmx.min.js
│   │   ├── routes/             # /health, /mvp_digest, /filter_new, /cluster,
│   │   │                       # /summarize, /admin/* (collections, voices,
│   │   │                       # runs, schedules, tts). /generate_tts is a
│   │   │                       # 410 Gone alias kept for one release.
│   │   ├── services/           # Business logic (called by routes AND activities)
│   │   │                       # incl. collections_repo, voices, temporal_admin
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

- [ ] Multi-user / per-user accounts (the admin GUI uses a single shared token)
- [ ] Vector database (Qdrant / Milvus)
- [ ] Web administration UI → **done in Phase 6** (FastAPI + Jinja2 + HTMX)
- [ ] Real-time streaming