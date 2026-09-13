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
│   │   │                       # /summarize, /generate_tts, /admin/* (collections,
│   │   │                       # voices, runs, schedules)
│   │   ├── services/           # Business logic (called by routes AND activities)
│   │   │                       # incl. collections_repo, voices, temporal_admin
│   │   ├── models/             # Pydantic schemas
│   │   └── db/                 # SQLite layer + migrations
│   ├── core/
│   │   ├── config.py           # pydantic-settings
│   │   ├── collections.py      # collections.toml loader (CollectionConfig)
│   │   ├── prompts.py          # LLM prompt templates + DIGEST_TEMPLATES registry
│   │   └── llm.py              # LiteLLM wrapper
│   └── workers/
│       ├── workflows.py        # DigestPipelineWorkflow + PipelineConfig
│       ├── activities.py       # RSS, filter, cluster, digest, TTS, Telegram,
│       │                       # record_run (run history)
│       ├── run_worker.py       # worker entry point
│       ├── trigger.py          # manual one-shot run (--collection)
│       └── schedule.py         # one Temporal schedule per collection
├── data/                       # SQLite DB + TTS cache (gitignored)
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