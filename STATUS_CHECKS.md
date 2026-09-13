# Status Checks — News Engine (deployed)

This guide walks through every layer of a deployed news-engine stack: Docker
containers, FastAPI, Temporal (server, UI, worker, schedules), external
backends (LLM, TTS), Telegram delivery, and the SQLite database.

Two ways to check:

1. **Script**: `./scripts/status.sh` — automated, prints one line per check,
   covers every section below, exit code `0` = all critical checks passed.
2. **Manual**: the commands below, to inspect things interactively.

All commands assume you are in the project root (where `docker-compose.yml`
and `.env` live). Replace `<name>` below with a real collection name from
`collections.toml`.

Everything here is read-only except **Section 10** (manual trigger), which
starts a real digest pipeline run.

---

## 0. Configuration reference (what the checks use)

| Setting | Source | Notes |
|---|---|---|
| `API_PORT` | `.env` | Host port of the FastAPI app (deployed: `8800`) |
| `LLM_API_BASE` | `.env` | llama.cpp/LiteLLM base URL (deployed: `http://192.168.5.4:9000/`) |
| `TTS_API_BASE_URL` | `.env` | OmniVoice base URL (deployed: `http://192.168.5.4:8000/api/v1`) |
| `TEMPORAL_HOST` | `.env` | Composes to `temporal:7233` (UI runs on host port `8080`) |
| `DATABASE_PATH` | `.env` | SQLite file resolved inside the containers |
| `TELEGRAM_BOT_TOKEN` | `.env` | Used for the `getMe` telegram check |

Docker Compose maps `${API_PORT:-8000}:8000`, so the API is **always** on
container port 8000; only the host port changes with `API_PORT`.

---

## 1. Docker containers

```bash
docker compose ps
```

Expected: 5 services `Up`:
`api`, `worker`, `temporal` (healthy), `temporal-postgres` (healthy),
`temporal-ui`.

```bash
docker compose ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'
```

Red flags:

- `Restarting` or high restart counts → crash-loop:
  `docker inspect news_engine_<svc> | grep RestartCount`
- `Created` but not `Up` → startup problem, read that service's logs
- `exited` on `worker`/`api` → serious runtime failure

## 2. FastAPI API

```bash
curl -sf "http://localhost:${API_PORT:-8800}/health"
```

Expected:

```json
{"status":"ok","service":"news-engine-api"}
```

The endpoint lives in `src/api/main.py` and only confirms the app booted
(including DB migrations, which run in the lifespan handler). If it fails:

```bash
docker compose logs api --tail 100
```

## 3. Temporal server + UI

```bash
curl -sf http://localhost:8080/api/namespaces
docker compose logs temporal --tail 50
```

A non-empty namespaces response means the Temporal frontend (gRPC :7233) and
the UI (:8080) are up. Server-side errors show up in the `temporal` logs.

Deep check — task queue reachable by the worker:

```bash
docker compose exec -T temporal temporal operator cluster describe --address temporal:7233
```

The command must point at the Temporal frontend service (`temporal:7233`); run
from the host it defaults to `localhost:7233`, which only works if port 7233
is published. A `ClusterName: active` row means the server is healthy.

UI: open http://localhost:8080 in a browser (namespace `default`,
task queue `news-engine-task-queue`).

## 4. Temporal worker

There is **no worker health endpoint**; the operational signals are logs and
the Temporal task queue. Check the worker actually started its pollers:

```bash
docker compose logs worker --tail 100 | grep -E "worker_running|starting_worker|connecting_to_temporal"
```

If `worker_running` is **absent**, the worker is either starting up, failing to
reach Temporal, or crash-looping — read the full log:

```bash
docker compose logs worker --tail 200
docker compose ps worker
```

Healthy: the log tail shows `connecting_to_temporal`, `starting_worker`,
`worker_running`, and (when a workflow runs) `Pipeline started`, `Pipeline
completed`.

## 5. Temporal schedules

Schedules are registered **per collection** from `collections.toml`:

```bash
docker compose exec -T temporal temporal operator schedule list --namespace default
```

Expect one `daily-digest-<name>` per enabled collection. To register/refresh
them (needs `collections.toml` to exist):

```bash
docker compose exec worker python -m src.workers.schedule            # all
docker compose exec worker python -m src.workers.schedule --collection tech
```

Got a `Schedule already exists` error? It's a harmless duplicate — schedules
are idempotent; run once and inspect the UI at
`http://localhost:8080/namespaces/default/schedules`.

## 6. External LLM backend

The LLM (llama.cpp via LiteLLM) is **not** part of the compose stack. Check it
directly. If `LLM_API_BASE` ends in `/v1` use `.../models`, otherwise use
`.../v1/models`:

```bash
LLM_API_BASE=$(grep '^LLM_API_BASE=' .env | cut -d= -f2)
case "${LLM_API_BASE%/}" in
  */v1) LLM_MODELS_URL="${LLM_API_BASE%/}/models" ;;
  *)    LLM_MODELS_URL="${LLM_API_BASE%/}/v1/models" ;;
esac
curl -sf "$LLM_MODELS_URL" | python3 -m json.tool
```

A response listing model ids (e.g. `Qwen3.5-4B`) means inference is reachable.
`curl` succeeding with an **HTML page** does *not* count — the script only
accepts valid JSON.
No endpoint? Do a real completion:

```bash
curl -sf "${LLM_MODELS_URL%/models}/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen3.5-4B","messages":[{"role":"user","content":"ping"}],"temperature":0.3}' \
  | python3 -m json.tool
```

Failures show up in the worker logs as `llm_call_failed`
(`src/core/llm.py`).

## 7. External TTS backend (OmniVoice)

```bash
curl -sf "http://192.168.5.4:8000/api/v1/health" | python3 -m json.tool
```

Expect the OmniVoice health JSON. The TTS step is **optional** per pipeline
run (controlled by `tts_enabled` in `collections.toml`); a failing TTS backend
does **not** break the text digest (workflow catches it and still delivers the
message — see `src/workers/workflows.py`).

## 8. Telegram delivery

Validate the bot token (never echo the token itself):

```bash
TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' .env | cut -d= -f2)
curl -sf "https://api.telegram.org/bot${TOKEN}/getMe" | python3 -m json.tool
```

`"ok": true` → token valid. `401` → token wrong/revoked. This only proves the
bot exists; to confirm a chat can receive it, run a manual pipeline
(Section 10) and check `delivery_status` in the printed result.

## 9. SQLite database

The DB lives inside the containers at `DATABASE_PATH`, backed by the
`./data:/app/data` volume (host path `data/news.db`). Inspect it through the
`api` container:

```bash
docker compose exec -T api python -c '
import sqlite3
from pathlib import Path
p = Path("'"$(grep '^DATABASE_PATH=' .env | cut -d= -f2)"'")
print("exists:", p.exists())
if not p.exists():
    raise SystemExit(0)
con = sqlite3.connect(p)
for name, sql in [
    ("articles",  "SELECT COUNT(*) FROM articles"),
    ("clusters",  "SELECT COUNT(*) FROM clusters"),
    ("deliveries","SELECT COUNT(*) FROM deliveries"),
    ("latest_fetched", "SELECT MAX(fetched_at) FROM articles"),
    ("latest_delivered","SELECT MAX(delivered_at) FROM deliveries"),
]:
    print(name + ":", con.execute(sql).fetchone()[0])
con.close()
'
```

Interpretation:

- `articles: 0` + `latest_fetched: None` → feeds were never fetched (network
  to RSS? disabled schedule?)
- `articles` growing but `deliveries` empty → articles are saved but no digest
  ever delivered (LLM failure, or Telegram creds bad)
- `latest_delivered` older than the schedule time → check worker logs and
  Temporal workflow history for that run

Host-side equivalent:

```bash
sqlite3 data/news.db "SELECT collection, COUNT(*) FROM articles GROUP BY collection;"
sqlite3 data/news.db "SELECT MAX(delivered_at) FROM deliveries;"
```

> Note: if your `.env` sets `DATABASE_PATH` to an **absolute** path like
> `/data/news.db`, it bypasses the `./data:/app/data` volume and data will NOT
> persist across `docker compose down`/restarts. The project default
> `data/news.db` is volume-backed.

## 10. End-to-end pipeline check (writes data + sends delivery)

The only true end-to-end test is running the digest workflow manually. It
fetches RSS, dedups, clusters, generates a digest, generates TTS (if enabled),
delivers to Telegram, and records rows in the DB:

```bash
docker compose exec worker python -m src.workers.trigger --collection tech
```

Optional flags: `--tts`, `--language es`, `--voice ...`,
`--feed https://...` (repeatable). Output is JSON:

```json
{
  "status": "ok",
  "article_count": 6,
  "cluster_count": 5,
  "digest": "...",
  "delivery_status": [{"status": "ok", "chat_id": "..."}]
}
```

Interpretation:

| Result | Meaning |
|---|---|
| `{"status":"no_new_articles","article_count":0}` | Healthy — nothing new since last run |
| `status: "ok"` + `delivery_status: [{"status":"ok",...}]` | Full pipeline healthy |
| `delivery_status: [{"status":"error",...}]` | Digest generated but Telegram delivery failed |

---

## Quick triage map

| Symptom | Check | Fix |
|---|---|---|
| `curl /health` hangs/fails | `docker compose ps api`, `logs api` | `docker compose restart api` |
| Worker never logs `worker_running` | `logs worker`, `ps worker` | Temporal down? `docker compose restart temporal` |
| No digest at schedule time | Section 5 (schedules), `logs worker` at that hour | `python -m src.workers.schedule` |
| `llm_call_failed` in logs | Section 6 | Fix `LLM_API_BASE` / restart llama.cpp |
| Digest sent but no audio | `logs worker` for TTS errors | MTTS backend (Section 7), TTS is optional |
| Telegram silent | Section 8 + `delivery_status` | Fix `TELEGRAM_BOT_TOKEN`/`CHAT_ID` |

## Useful one-liners

```bash
# One-pass sanity: API + UI + worker + LLM + TTS
./scripts/status.sh

# Tail all logs
docker compose logs -f api worker temporal temporal-ui temporal-postgres

# Last workflow runs for a schedule in the UI
#   http://localhost:8080/namespaces/default/schedules/daily-digest-tech
```