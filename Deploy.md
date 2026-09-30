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
   - **Preferred**: `ADMIN_USERNAME` + `ADMIN_PASSWORD_HASH` (bcrypt). The login
     form will use username/password instead of the token. Generate the hash
     with: `python -c "import bcrypt; print(bcrypt.hashpw(b'password', bcrypt.gensalt()).decode())"`
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
- `worker` — Temporal worker polling the digest task queue
- `narration-worker` — Temporal worker polling the narration task queue
  (multi-speaker audio; Phase 7)
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

> **Editing `collections.toml` on a deployed host:** the file is bind-mounted,
> so a running container keeps reading the *old* file it was started with.
> `sed -i`, `git pull` and most editors replace the file, and the worker keeps
> serving the previous content — a changed `narration_spec` (voices, `speed`)
> or `enabled` flag then looks like it was ignored. Prefer editing in place,
> and after any edit recreate the containers and re-register the schedules:

```bash
docker compose up -d --build api worker narration-worker
docker compose exec worker python -m src.workers.schedule --collection <name>
```

### Narration (Phase 7)

From 7.3+, the same digest can also produce a **multi-speaker narration**
audio piece (M4A with music bed). The narration runs on its own task queue
and has its own worker (`narration-worker`).

Enable it per-collection in `collections.toml`:

```toml
[[collections]]
name = "tech"
# ... other fields ...
narration_enabled = true
narration_strategy = "turns"          # "turns" (default) or "conversations"
narration_spec = { title = "Tech Brief", language = "es", roles = [
    {name="anchor", voice_id="es-mx-male"},
    {name="expert", voice_id="es-mx-female"}
], pause_ms = 400, music = {track="bed.mp3", volume_db=-18, fade_in_ms=2000, fade_out_ms=3000} }
```

Global kill switch: `NARRATION_ENABLED=false` in `.env` disables narration
for all collections.

The narration spec fields:
- `roles`: array of 1-8 roles with `name`, `voice_id`, optional `speed`,
  `emotion`, `language`
- `music`: optional local file in `NARRATION_MUSIC_DIR` with fades and ducking
- `pause_ms`: silence between turns (0-5000 ms)
- `narration_strategy`: "turns" (one request per segment) or "conversations"
  (single request; requires homogeneous cast — no per-role speed/emotion)

The narration worker uses the same TTS gateway as the digest TTS
(`NARRATION_API_BASE_URL` or `TTS_API_BASE_URL` as fallback) and writes
audio to `NARRATION_CACHE_DIR` (`data/narration/`), separate from the digest
TTS cache.

### Narration delivery (Phase 7.6)

When a collection has `narration_enabled = true` and `narration_deliver_audio = true`
(default), the generated narration M4A is **delivered** to the collection's
`delivery_channels` (Telegram audio, email attachment, WhatsApp audio). This is
in addition to the text digest and the optional TTS audio.

If the narration render fails, an **error notification** is sent to the same
channels instead of silently substituting a stock Edge voice.

For **dialog/podcast renders** (two or more distinct voices, or the
`conversations` strategy), the fallback is controlled by
`NARRATIONS_DIALOG_FALLBACK_TO_EDGE` (default `false`). This ensures cloned
voices (e.g. Dwight/MichaelScott) are never silently replaced — the render
fails and the error alert fires. The general `NARRATION_FALLBACK_TO_EDGE`
still governs single-voice narrations.

### Manual narration render (admin API / CLI)

Trigger a one-off narration from the GUI (`POST /api/admin/narration`) or
CLI:

```bash
# With inline script (JSON files)
docker compose exec worker python -m src.workers.trigger_narration \
  --collection tech --spec spec.json --script script.json

# With stored script
docker compose exec worker python -m src.workers.trigger_narration \
  --collection tech --spec spec.json --script-id 12
```

The narration worker is started automatically by `docker compose up` (see
service `narration-worker` in `docker-compose.yml`).

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

`python -m src.workers.schedule` also registers (or updates) the two **monitor
schedules** automatically:

- `collections-config-monitor` — wakes `CollectionsMonitorWorkflow` every
  `COLLECTIONS_MONITOR_INTERVAL_MINUTES` to re-register digest schedules when
  `collections.toml` changes (and alerts when a collection is added/removed).
- `system-monitor` — wakes `SystemMonitorWorkflow` every `MONITOR_INTERVAL_MINUTES`
  to run the health checks and alert on problems.

---

## 4.0 System Monitoring

The worker mounts the **Docker socket read-only** so the monitor can inspect the
containers (see `docker-compose.yml`, `worker` service):

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock:ro
```

Required `.env` entries (all optional except telegram/email when those channels
are enabled):

```bash
MONITOR_INTERVAL_MINUTES=15            # ticks of SystemMonitorWorkflow
MONITOR_RETENTION_DAYS=3               # SQLite monitor_events retention
MONITOR_LOG_RETENTION_DAYS=3           # JSONL logs in data/logs/
MONITOR_NOTIFY_CHANNELS=["telegram"]   # telegram | email | whatsapp
MONITOR_TELEGRAM_CHAT_ID=              # overrides TELEGRAM_CHAT_ID for alerts
MONITOR_EMAIL_RECIPIENTS=["you@x.com"] # for the email channel
MONITOR_WHATSAPP_RECIPIENTS=[]         # phone numbers for the whatsapp channel
MONITOR_WHATSAPP_GROUP_ID=
MONITOR_DISK_THRESHOLD_PERCENT=20      # min % free before warning (warn when >80% full)
MONITOR_DAILY_SUMMARY=true             # once-per-UTC-day full summary
MONITOR_ALERT_ON_RUN=false             # notify each completed digest run
MONITOR_TTS_DISK_PATH=disk             # GET <tts_api_base_url>/disk
MONITOR_DOCKER_SOCKET=/var/run/docker.sock  # "" disables the docker check
```

The `tts_server_disk` check expects `GET <tts_api_base_url>/disk` to return
`{"total": int, "free": int, "used": int}` (bytes); until the TTS server exposes
it, the check reports `skipped`. Alerts with no channels configured are recorded
in the DB/log only.

Verify a manual monitor run:

```bash
docker compose exec worker python -c "
import asyncio
from src.api.services.monitoring.checks import run_all_checks
async def main():
    for r in await run_all_checks():
        print(r.status, r.check, r.detail)
asyncio.run(main())
"
```

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

---

## 7. Modal (Phase 7.8)

The Modal worker is declared in `docker-compose.yml` behind the `modal` profile,
so it is created but **not started**. No collection uses a Modal backend yet,
so there is no spend.

### Enable Modal

1. Set the credentials in `.env`:
   ```bash
   MODAL_ENABLED=true
   MODAL_TOKEN_ID=...
   MODAL_TOKEN_SECRET=...
   ```
2. Start the Modal worker:
   ```bash
   docker compose --profile modal up -d narration-modal-worker
   ```
3. Opt a collection into a Modal backend in `collections.toml`:
   ```toml
   narration_tts_backend = "qwen3_tts"   # or "zonos2" once the gate passes
   narration_voice_source = "qwen_design"
   ```

### Generate the reference voices

```bash
docker compose exec narration-modal-worker python -m src.workers.design_voice --all --upload
```

This runs `VoiceCatalogWorkflow`, which designs every missing or stale profile
with Qwen3-TTS VoiceDesign, stores the reference in `data/voices/refs/`, records
it in `data/voices/manifest.json` and uploads it to the Modal Volume.

### Budget

The guard enforces `MODAL_BUDGET_USD_DAILY` (default $1) and
`MODAL_BUDGET_USD_MONTHLY` (default $10) before any `.spawn()`. A cache hit is
always allowed. The `modal_budget` monitor check alerts at 50/80/100%.

### Rollback

```bash
docker compose --profile modal down
# and set MODAL_ENABLED=false, or revert narration_tts_backend to "omni_voice"
```

`collections.toml` is bind-mounted on the server (inode 461207). Changing
branches without checking that bind mount is the known footgun — see commit
`80a19a5`.