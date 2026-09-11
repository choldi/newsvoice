# Deployment Guide — AI News Digest Engine

This guide details instructions for deploying and running **news-engine**.

Stack: FastAPI API, a Temporal.io server + UI, and a Python worker running the
digest workflow. Orchestration/scheduling is handled by Temporal, not cron.

---

## 1. Prerequisites

- Server with Linux OS (Ubuntu 22.04 LTS or similar recommended)
- Docker & Docker Compose installed
- Local LLM inference server (e.g. `llama.cpp` server) accessible via HTTP API
  (`/v1/chat/completions`)
- A Telegram bot token and chat ID for delivery

---

## 2. Configuration Setup

1. Clone the repository to the production server.
2. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
3. Update `.env` with production values:
   - `LLM_API_BASE` — URL of your llama.cpp instance
   - `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID`
   - `RSS_FEED_URLS` — JSON list of feed URLs
   - `TEMPORAL_HOST` & `TEMPORAL_TASK_QUEUE` (defaults work for the bundled
     Temporal server; set `temporal:7233` for container-to-container access)

---

## 3. Starting Services

Using Docker Compose:
```bash
docker compose up -d --build
```

This starts:
- `api` — FastAPI on `:8000`
- `worker` — Temporal worker polling the dev task queue
- `temporal` — Temporal server on `:7233`
- `temporal-ui` — Temporal web UI on `:8080`
- `temporal-postgres` — Postgres backing the Temporal server

Verify service status:
```bash
docker compose ps
```

---

## 4. Registering the Daily Schedule

Once the worker is running, register the daily schedule (08:00 UTC by default):

```bash
docker compose exec worker python -m src.workers.schedule --hour 8 --minute 0
```

Optional: enable TTS audio in the scheduled runs with `--tts`.

To run a one-off digest manually (useful for debugging):

```bash
docker compose exec worker python -m src.workers.trigger --tts
```

---
## 5. Health Checks

Check API health:
```bash
curl http://localhost:8000/health
```
Expected response:
```json
{"status": "ok", "service": "news-engine-api"}
```

Check Temporal services:
```bash
curl -s http://localhost:8080/api/namespaces           # Temporal UI/API
docker compose logs -f temporal                        # Temporal server logs
```

---
## 6. Rollback & Maintenance

To view logs:
```bash
docker compose logs -f api
docker compose logs -f worker
docker compose logs -f temporal
```

To stop services:
```bash
docker compose down
```

To restart the API only:
```bash
docker compose restart api
```

To cancel/remove a schedule from the CLI or UI:
```bash
docker compose exec worker python -m src.workers.trigger   # manual runs only
```
Delete schedules through the Temporal UI at `http://localhost:8080/namespaces/default/schedules`.