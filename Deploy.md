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
   - `RSS_FEED_URLS` — JSON list of feed URLs (fallback for the implicit default
     collection when no `collections.toml` is present)
   - `COLLECTIONS_FILE` — path to the collections config (default `collections.toml`)
   - `TEMPORAL_HOST` & `TEMPORAL_TASK_QUEUE` (defaults work for the bundled
     Temporal server; the Compose `api`/`worker` services override it to
     `temporal:7233` for container-to-container access)
   - `ADMIN_API_TOKEN` — token for the admin web GUI (`/ui`) and the admin REST
     API (`/api/admin/*`). Leave empty to keep the GUI locked (secure default).
     Generate one with `openssl rand -hex 24`.
4. Copy `collections.toml.example` to `collections.toml` and define your
   collections (names, feed URLs, per-collection schedule `time`, TTS flags,
   prompt templates, optional per-collection `chat_id`)

---

## 3. Starting Services

Using Docker Compose:
```bash
docker compose up -d --build
```

This starts:
- `api` — FastAPI on `:8000` (serves the REST API, `/api/admin/*`, and the
  admin GUI at `/ui`)
- `worker` — Temporal worker polling the dev task queue
- `temporal` — Temporal server on `:7233`
- `temporal-ui` — Temporal web UI on `:8080`
- `temporal-postgres` — Postgres backing the Temporal server

Verify service status:
```bash
docker compose ps
```

### Quick reference (`make`)

Most deploy/recurring actions have a `make` wrapper (run `make help` for the
full list):

| Action | Command |
|--------|---------|
| Start/build services | `make up` |
| Health + status checks | `make health` / `make status` |
| Tail logs | `make logs` / `make logs-api` / `make logs-worker` |
| Register schedules | `make schedule` |
| Manual run | `make trigger` |
| Deploy | `make deploy` (default `production`) |

### Admin GUI

Log in at `http://<host>:8000/ui/login` with `ADMIN_API_TOKEN`. From there you
can, without touching code or `collections.toml` by hand:

- Create / edit / delete collections (feeds, time, TTS, podcast identity)
- See the weekly schedule calendar and resync Temporal schedules
- Browse run history and launch an immediate run
- Manage voices (stock voices and cloning new ones from an audio reference)

---

## 4. Registering the Schedules

Once the worker is running, create **one schedule per collection**. Collections
are defined in `collections.toml` (copy `collections.toml.example`), each with
its own name, feed URLs, delivery options, and `time` (HH:MM UTC):

```bash
cp collections.toml.example collections.toml
# edit collections.toml: names, feeds, times
docker compose exec worker python -m src.workers.schedule
```

This registers `daily-digest-<name>` schedules for every enabled collection.
Target a single collection with `--collection <name>`.

Optional: force-enable TTS audio in all scheduled runs with `--tts`.

To run a one-off digest manually (useful for debugging):

```bash
docker compose exec worker python -m src.workers.trigger --collection tech
```

> Note: `--tts` forces TTS/audio generation for the manual run; without it the
> flag defaults to `false` regardless of `tts_enabled` in `collections.toml`.
> Scheduled runs always honor the collection's own `tts_enabled`.

With no `collections.toml`, the legacy single-schedule behavior applies:
`python -m src.workers.schedule --hour 8 --minute 0` creates one daily digest
from `RSS_FEED_URLS`.

---

## 4.1 WhatsApp Delivery

WhatsApp delivery uses a self-hosted Baileys gateway
(`blakegt/whatsapp-web-api-rest`, compose service `there`) that translates a
JSON HTTP API into WhatsApp via the linked phone's session. The gateway:

- listens internally on `:8085` (`POST /message` for text/media,
  `POST /simulate` for the typing indicator) and is **not** exposed to the LAN
  — it has no auth, so it must only be reachable by the worker.
- persists its Baileys session in `data/there` (mounted at `/app/auth_info`,
  container user is uid 1001).

Enable it in `.env`:

```bash
WHATSAPP_API_URL=http://there:8085
WHATSAPP_API_TOKEN=<any non-empty value>   # fail-closed kill switch; gateway has no auth
WHATSAPP_ALLOWLIST=["+34612345678"]        # E.164 numbers allowed to receive digests
```

Then in `collections.toml`, per collection:

```toml
whatsapp_recipients = ["+34612345678"]   # must be in WHATSAPP_ALLOWLIST
delivery_channels = ["telegram", "email", "whatsapp"]
```

Groups are always allowed: `whatsapp_group_id = "1234567890@g.us"`.

**First-time pairing (QR):** the gateway acts as a WhatsApp Web client, so it
must be linked to a phone once. From your machine, tunnel the internal port and
open the gate way's page to scan the QR:

```bash
docker run -d --rm --name gw_pair_proxy -p 127.0.0.1:8085:8085 \
  --network news-summary_default alpine/socat TCP-LISTEN:8085,fork TCP:there:8085
ssh -L 8085:localhost:8085 user@server   # from your laptop, keep open
# open http://localhost:8085 in a browser, then in WhatsApp:
#   Settings -> Linked devices -> Link a device  (scan the QR)
docker rm -f gw_pair_proxy               # remove the temporary proxy after pairing
```

Session state persists in `data/there`, so the pairing survives container
restarts. Re-pair at any time by repeating the tunnel+scan steps.

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