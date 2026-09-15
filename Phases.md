# PHASES.MD - Incremental Delivery Plan (Temporal-based)

Each phase is **independently shippable**. Do NOT start phase N+1 until N is
running reliably for **3+ days**.

---

## PHASE 1 - Foundations and Temporal Bootstrap

**Goal:** Restore a healthy codebase and replace the n8n hub with Temporal.

**Scope:**
- [x] Fix broken codebase (health endpoint, route prefixes, service imports)
- [x] Add `temporalio` to `pyproject.toml` and install in the venv
- [x] Docker Compose: `temporal` (server) + `temporal-ui` + `temporal-postgres`
- [x] Remove n8n from `docker-compose.yml`
- [x] Worker skeleton: `src/workers/run_worker.py`, `src/workers/activities.py`
- [x] `.env.example` documented with `TEMPORAL_*` variables

**Success criteria:** `docker compose up` brings up Temporal on `:7233` and the
API on `:8000`; `curl localhost:8000/health` returns `{"status": "ok"}`.

**Effort:** 4-6 hours.

---

## PHASE 2 - RSS and Core Activities

**Goal:** Every step of the digest pipeline is a Temporal activity with a single
responsibility.

**Scope:**
- [x] `fetch_rss_activity` (feedparser, async, per-feed timeout)
- [x] `filter_new_activity` (SQLite dedup, 7-day window)
- [x] `cluster_activity` (entity+action fingerprint grouping)
- [x] `generate_digest_activity` (LiteLLM → 5+5 markdown)
- [x] `generate_tts_activity` (OmniVoice API, fragmentado, content-hash cache)
- [x] `deliver_telegram_message_activity` / `deliver_telegram_audio_activity`
- [x] Activity unit tests with `ActivityEnvironment`

**Success criteria:** `test_activities.py` passes; each activity is callable
from an `ActivityEnvironment` in isolation.

**Effort:** 6-8 hours.

---

## PHASE 3 - Workflow Orchestration and Scheduling

**Goal:** A durable workflow runs the full pipeline on a daily schedule.

**Scope:**
- [x] `DigestPipelineWorkflow` choreographing fetch → filter → cluster → digest
  → TTS → deliver
- [x] Early exit when no new articles
- [x] Configurable TTS/audio delivery via `PipelineConfig`
- [x] Per-activity start-to-close timeouts
- [x] Worker service in `docker-compose.yml` (registers workflow + activities)
- [x] `trigger.py` (manual one-shot run)
- [x] `schedule.py` (native Temporal schedule, daily 08:00 UTC)
- [x] Workflow integration tests with `WorkflowEnvironment` + stub activities

**Success criteria:** A started workflow returns a digest dict end-to-end in the
test environment; the schedule starts one workflow per day.

**Effort:** 8-10 hours.

---

## PHASE 4 - Documentation and Cleanup

**Goal:** Remove all n8n traces and make the docs describe the Temporal design.

**Scope:**
- [x] Rewrite this `Phases.md` from scratch
- [x] Update `Architecture.md` (Temporal hub, new data flow, project tree)
- [x] Update `CONVENTIONS.md` (drop n8n section, add Temporal conventions)
- [x] Update `Deploy.md` (Temporal stack, worker, schedule, trigger)
- [x] Update `AGENTS.md` (Temporal architecture, commands, examples)
- [x] Delete `n8n/` directory
- [x] Remove n8n references from code docstrings
- [x] Final verification: ruff + pytest + `docker compose config`

**Success criteria:** No `n8n` reference remains in code, config, or docs — except
historical notes describing the migration itself.

**Effort:** 2-3 hours.

---

## PHASE 5 - Multiple Collections and Schedules

**Goal:** Separate digests for "Tech", "Health", "Finance".

**Scope:**
- [x] `collections.toml` (inspired by Better Morning)
- [x] Per-collection `PipelineConfig` (feeds, TTS flag, delivery target)
- [x] One Temporal schedule per collection at a configurable time
- [x] Shared DB, collection-scoped dedup
- [x] Per-collection prompt templates

**Success criteria:** 3 independent Telegram digests at different times.

**Effort:** 6-8 hours.

---

## PHASE 6 (Optional) - Web UI and Multi-user

**Goal:** Manage sources and view history from a browser.

**Scope:**
- [x] FastAPI + HTMX or simple React frontend
- [x] Auth (basic token)
- [x] CRUD for collections and feeds
- [x] History + executions viewer with live Temporal status and stop button

**Success criteria:** A non-technical user can add an RSS feed without touching
code.

**Effort:** 20-30 hours.

> **Only if Phase 1-5 are stable.**

**Observability decision (evaluated, done):** zigflow was evaluated and rejected
(its YAML DSL compiles to a separate Go worker, incompatible with the Python SDK
workflows + `WorkflowEnvironment` tests). Temporal Flow is an optional desktop
(graph) viewer, not a container overlay: it connects to the existing
`temporal-ui` at `:8080` with no code or compose changes. Default debugging stays
on the built-in Temporal UI (Compact + Timeline views). See Architecture.md →
Observability.

---

## ANTI-PATTERNS TO AVOID

- Do NOT add collections/multi-user before the single pipeline is stable
- Do NOT use Postgres for application state in Phase 1-4 (Temporal's own
  `temporal-postgres` is fine; news-engine state stays in SQLite)
- Do NOT build a web UI before Phase 5
- Do NOT call LLM SDK directly from routes or workflows (always via LiteLLM)
- Do NOT perform network I/O inside workflows (lives in activities)
- Do NOT reintroduce APScheduler/cron while Temporal schedules are available