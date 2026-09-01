# AI News Digest Engine — Overview

A self-hosted modular system that produces a daily AI-curated news digest (5 main stories + 5 headlines) from RSS feeds, using a local LLM (`llama.cpp`) and delivering via **Telegram** / **Email**.

---

## Core Principle: Hub and Spoke

| Role | Description | Technology |
|------|-------------|------------|
| **Hub (n8n)** | Orchestration, scheduling, retries, notifications | n8n (Docker, self-hosted) |
| **Spokes (FastAPI microservices)** | Each Python endpoint owns **ONE responsibility** | FastAPI + Python 3.11+ |

---

## Components

| Component | Role | Technology |
|-----------|------|------------|
| **orchestrator** | Scheduling, flow control, delivery | n8n (Docker, self-hosted) |
| **api** | Business logic, LLM calls, state | FastAPI + Python 3.11+ |
| **llm** | Local inference server | `llama.cpp` (`/v1/chat/completions`) |
| **db** | Delivery history, dedup state | SQLite (`news.db`) |
| **storage** | Generated audio, cached articles | Local filesystem (`./data/`) |

---

## Data Flow (Full Pipeline)

```
[n8n Schedule 08:00]
    |
    v
[n8n RSS Read] --> [POST /api/filter_new] --> [SQLite: unseen?]
    |
    v
[POST /api/cluster] --> [group by event fingerprint]
    |
    v
[POST /api/summarize] --> [llama.cpp → 5+5 markdown]
    |
    v
[POST /api/generate_tts] --> [edge-tts → .mp3]
    |
    v
[n8n Telegram: text + audio]
```

---

## Key Decisions

| Decision | Rationale |
|----------|-----------|
| **n8n over cron/GitHub Actions** | Visual retries, easy output switching, API-driven |
| **FastAPI over monolithic script** | Each phase = one endpoint, independently testable |
| **SQLite over Postgres** | Single file, zero ops, sufficient for personal use |
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
├── pyproject.toml
├── docker-compose.yml          # n8n + api
│   ├── .env.example
│   └── src/
│       ├── api/
│       │   ├── main.py          # FastAPI app
│       │   └── routes/
│       │       ├── ingest.py   # /filter_new
│       │       ├── cluster.py   # /cluster
│       │       ├── summarize.py # /summarize
│       │       ├── tts.py       # /generate_tts
│       │   └── services/        # Business logic
│       │       ├── models/      # Pydantic schemas
│       │       └── db/          # SQLite layer
│       └── core/
│           ├── config.py        # pydantic-settings
│           └── llm.py           # LiteLLM wrapper
├── data/                        # SQLite DB + TTS cache (gitignored)
├── n8n/
│   └── workflows/               # Exported JSON workflows
├── tests/
│   ├── unit/
│   └── integration/
└── ...
```

---

## Non-Goals (v1)

- [ ] Multi-user / authentication
- [ ] Vector database (Qdrant / Milvus)
- [ ] Web administration UI
- [ ] Real-time streaming