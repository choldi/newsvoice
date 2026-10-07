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
   - `LLM_API_BASE` — endpoint OpenAI-compatible del LLM. En producción:
     `http://100.75.148.68:11434/v1` con `LLM_MODEL=openai/qwen3:8b` (Ollama de
     debwk, el LLM migrado; modelos: `qwen3:8b`, `llama3.1:8b`, `phi3:mini`,
     `nemotron-cascade-2`). Fallback si debwk cae:
     `http://192.168.5.4:9000/v1` (Qwen3.5-4B en winsrv, verificado).
     **No uses `localhost:8080` en esta caja: ahí vive qBittorrent.**
   - `TTS_API_BASE_URL` — endpoint del gateway OmniVoice (voces, clonado y
     render de narración/lecciones): `http://debwk.elver-exponential.ts.net:8001/api/v1`.
     **No uses el `:8000` antiguo** (API vieja, ya parada): con él la GUI de
     voces sale vacía y los renders no encuentran el gateway.
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
   Nota de topología: **debwk (`100.75.148.68`,
   `debwk.elver-exponential.ts.net`) es la instancia autoritativa** del pack:
   UI, workers, Temporal y datos viven allí desde la migración (desde cualquier
   caja de la tailnet: `http://debwk.elver-exponential.ts.net:8000/ui`).
   La copia de vm-ubuntu quedó **parada** para evitar digestos duplicados
   (`docker compose stop` + `restart=no`); para revertir la migración y
   volver a ejecutarla desde vm-ubuntu:
   `cd ~/aider_projects/news_sumary && docker compose start && for n in $(docker ps -aq --filter name=news_engine_); do docker update --restart=unless-stopped $n; done`
   (y parar la de debwk con `ssh debwk 'cd ~/news-summary && docker compose stop'`).
   El `data/` de debwk es root-owned: las copias de estado se hacen con
   `rsync` a `~/news-transfer/` y un `docker run ... cp -a` (sin sudo).

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
- `temporal-ui` — Temporal web UI on `:8081` (host port; the container listens on 8080)
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

**URL desplegada (producción):** `http://debwk.elver-exponential.ts.net:8000/ui`
(Tailscale MagicDNS, accesible desde el móvil; en LAN `http://debwk:8000/ui` si
tienes el nombre local resuelto). La GUI solo es remota cuando la caja expone
`:8000` en la interfaz elegida.

Log in at `http://<host>:8000/ui/login` with `ADMIN_API_TOKEN`. From there you
can, without touching code or `collections.toml` by hand:

- Create / edit / delete collections (feeds, time, TTS, podcast identity,
  narration cast editor, verification options)
- See the weekly schedule calendar and resync Temporal schedules
- Browse run history (with the verification ledger per run) and launch an
  immediate run
- Manage voices (stock voices and cloning new ones from an audio reference)
- **Herramientas** (`/ui/tools`): URL summary, document TTS, language lessons
  and manual podcast renders, with live job tables
- **Ajustes** (`/ui/settings`): validated editor for the whole `.env`

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

> **The narration gateway is OmniVoice on port 8001, not the news-engine API
> on 8000.** Leave `NARRATION_API_BASE_URL` empty (it then reuses
> `TTS_API_BASE_URL`); if you set it, use `:8001`. Pointing it at `:8000`
> makes the worker POST `/api/v1/tts` to the news-engine API, which has no such
> route, and every render dies with `NarrationGatewayError: Not Found`.
> `Settings` refuses to start with that misconfiguration (see
> `narration_base_url` in `src/core/config.py`).

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
docker compose exec -T temporal temporal operator namespace list \
  --address temporal:7233   # list namespaces
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
Delete schedules through the Temporal UI at `http://localhost:8081/namespaces/default/schedules`.

To inspect or clear the ingested news (articles and clusters) while keeping the
run and delivery history:

```bash
docker compose exec -T api python - --inspect < scripts/news_reset.py
docker compose exec -T api python - --purge    < scripts/news_reset.py
```

`--purge` writes a `data/news.db.pre-purge-<timestamp>` backup first and only
removes `articles`, `clusters` and `cluster_articles`, so the next digest
ingests and picks fresh stories. It has to run inside a container: the
containers write `news.db` as root and the host user can only read it.

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

Design left Modal in Phase 7.9: the reference voices are generated on the GPU
box, and only the clone upload still needs Modal. See
[section 8](#8-voice-catalog-phase-79).

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
---

## 8. Voice catalog (Phase 7.9)

`VoiceCatalogWorkflow` designs the reference voices the clone backends read.
The whole flow runs in its own Temporal namespace, `news-engine-voices`,
split across two workers — a Temporal activity always executes in the
namespace of the workflow that scheduled it, so both halves must live there:

| Role | Runs on | Queue | Registers |
| --- | --- | --- | --- |
| `--role catalog` | the server (compose) | `VOICE_CATALOG_TASK_QUEUE` | `VoiceCatalogWorkflow` + status/store/upload |
| `--role design` | the GPU box (debwk) | `VOICE_DESIGN_TASK_QUEUE` | `design_voice_activity` |

The design worker synthesizes with Qwen3-TTS VoiceDesign and returns the audio
gzipped over the wire, so it never touches `data/voices`: only the catalog
worker writes `data/voices/refs/*.wav` and `manifest.json`.

### 8.1 One-time: create the namespaces

```bash
./scripts/create_namespaces.sh
```

That creates the three namespaces the workers need and is idempotent:

| Namespace | Owner | Queue |
| --- | --- | --- |
| `default` | digest + system monitor | `TEMPORAL_TASK_QUEUE` |
| `news-engine-voices` | voice catalog + voice design | `VOICE_CATALOG_TASK_QUEUE`, `VOICE_DESIGN_TASK_QUEUE` |
| `news-engine-podcast-local` | local podcast render | `NARRATION_LOCAL_TASK_QUEUE` |
| `news-engine-podcast-modal` | Modal podcast render | `NARRATION_MODAL_TASK_QUEUE` |

Re-run it after any Temporal restore: the namespaces live in Postgres, not in
the repo, so a fresh `temporal_pg_data` comes back with `default` only. A
missing namespace is silent — the worker polls an empty namespace forever and
the digest completes without audio.

### 8.2 Server: the catalog worker

```bash
docker compose up -d voice-catalog-worker
docker compose logs -f voice-catalog-worker
```

Without it the admin trigger starts a workflow nobody picks up. The API route
that starts it already connects with
`build_client(settings.temporal_voices_namespace)`, so the namespace above
must exist first.

It also serves the staging endpoint the design box uploads to, so the server
`.env` needs a shared key (empty disables the endpoint):

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"   # once
echo "VOICE_STAGING_API_KEY=<the value above>" >> ~/news-summary/.env
docker compose up -d api voice-catalog-worker
```

### 8.3 The design worker (same box, off by default)

Since the consolidation the server and the GPU are the **same machine**
(debwk, `100.75.148.68`), so the design worker is a **user-level** systemd
service on debwk, not a container and not a second box: Docker there has no
NVIDIA runtime registered, while the CUDA stack already lives in a venv
(`torch 2.14.1+cu130`, `qwen_tts 0.1.1`).

One-time setup:

```bash
cd ~/news-summary

# 1. the Temporal client plus httpx for the staging upload, on top of the CUDA
#    stack already in that venv
~/qwen3-tts/venv/bin/pip install temporalio structlog pydantic-settings httpx

# 2. worker unit: same box, so Temporal and the staging API are on localhost
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/news-engine-voice-worker.service <<'UNIT'
[Unit]
Description=news-engine voice design worker (Temporal)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=%h/news-summary
Environment=PYTHONUNBUFFERED=1
Environment=TEMPORAL_HOST=127.0.0.1:7233
# The design activity POSTs the WAV to the server API, which is this same box.
Environment=VOICE_STAGING_URL=http://127.0.0.1:8000
Environment=VOICE_STAGING_API_KEY=<same value as VOICE_STAGING_API_KEY in .env>
ExecStart=%h/qwen3-tts/venv/bin/python -m src.workers.run_voice_worker --role design
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
loginctl enable-linger "$USER"   # keep it up after logout
```

**The unit is deliberately not enabled.** Voice design needs the whole GPU, and
so do the TTS gateway and Ollama, so the design worker stays stopped and
`scripts/design_window.sh` is the only thing that starts it:

```bash
./scripts/design_window.sh          # stops OmniVoice, unloads Ollama, runs the
                                    # design worker until the voices namespace
                                    # has no workflow left, then restores both
./scripts/design_window.sh 600      # same, with an explicit 600s budget
```

That way the daily digests and the narration always have the GPU, and the
design window is the only thing that competes for it. Check the state with:

```bash
systemctl --user is-active tts-api.service news-engine-voice-worker.service
journalctl --user -u tts-api.service -f
```

`voice_design_task_queue` and `temporal_voices_namespace` keep their defaults,
so the unit only has to set `TEMPORAL_HOST` and the staging pair. The first run
downloads `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign` (~4 GB) into the HuggingFace
cache.

### 8.4 Generating the voices

```bash
docker compose exec voice-catalog-worker python -m src.workers.design_voice --all --upload
```

Same CLI as before, but it now starts `VoiceCatalogWorkflow` in
`news-engine-voices`. It designs every missing or stale profile, stores each
reference in `data/voices/refs/`, records it in `data/voices/manifest.json`
and uploads it to the Modal Volume for the clone backends. Cost is recorded as
`modal_cost_usd: 0.0`: design no longer runs on Modal.

The audio itself never goes through Temporal: the design activity stages it at
`POST /api/internal/voices/staged/{name}` and the store activity reads it back
from `data/voices/.staging/`. Only the sha256 crosses the workflow history, so
a full 24-voice batch no longer trips the server's history size limit. An
upload to Modal is attempted **once**: it fails on configuration, not on
flakiness, and an endless retry would wedge the workflow.

#### Recovery after a migration

`data/voices/` is git-ignored, so a new machine has no designed references.
Copy them from wherever the design ran and register them in the gateway
(idempotent; trims the refs that exceed the gateway's 30 s limit):

```bash
rsync -a <old-host>:/path/to/news-summary/data/voices/ ./data/voices/
.venv/bin/python scripts/clone_designed_voices.py
```

### 8.5 Rollback

```bash
docker compose down voice-catalog-worker
systemctl --user disable --now news-engine-voice-worker   # on the GPU box
```

The catalog keeps working from the references already in `data/voices/`.
---

## 9. Podcast namespaces (Phase 7.9b)

Podcast generation owns one namespace per backend. A Temporal activity always
executes in the namespace of the workflow that scheduled it, so the digest
cannot keep calling the render activities directly: it calls a single
`dispatch_narration_activity` that starts and awaits `NarrationWorkflow` in the
namespace that owns the backend.

| Role | Namespace | Queue | Registers |
| --- | --- | --- | --- |
| `worker` | `default` | `TEMPORAL_TASK_QUEUE` | `DigestPipelineWorkflow` + digest activities + `dispatch_narration_activity` |
| `narration-worker` | `news-engine-podcast-local` | `NARRATION_LOCAL_TASK_QUEUE` | `NarrationWorkflow` + resolve/render |
| `narration-modal-worker` | `news-engine-podcast-modal` | `NARRATION_MODAL_TASK_QUEUE` | `NarrationWorkflow` + resolve/render |
| `voice-catalog-worker` | `news-engine-voices` | `VOICE_CATALOG_TASK_QUEUE` | see §8 |

```bash
./scripts/create_namespaces.sh   # once, and after any Temporal restore
docker compose up -d worker narration-worker voice-catalog-worker
docker compose --profile modal up -d narration-modal-worker   # needs Modal creds
```

`src/workers/podcast_target.py` maps a spec's `tts_backend` to its namespace
and queue, and it is shared by the schedule, the API trigger and the CLI
trigger, so all three agree on where a render happens. `dispatch_narration_activity`
keys the workflow id as `narration-<collection>-<run_id>`: an activity retry
reattaches to the render already in flight instead of paying for a second one.

Check that every worker registered where it should:

```bash
docker compose exec -T temporal temporal operator namespace list \
  --address temporal:7233
docker compose logs worker | grep -i "connected\|namespace"
```

### 9.1 Rollback

```bash
docker compose stop worker narration-worker   # renders stop, digests continue
```

Without the podcast workers the digest still completes and records the
narration delivery as `error`, so this degrades instead of failing.

## 10. Local TTS gateway (OmniVoice)

The local narration backend is the **OmniVoice gateway** (`~/ov_api`), not the
API container and not genpod: it is the only one that serves the contract
news-engine calls (`/api/v1/health`, `/api/v1/voices/stock`,
`/api/v1/voices/clone`, `/api/v1/tts`, `/api/v1/conversations`). It runs as a
**user-level** systemd service on the host, because Docker on this box has no
NVIDIA runtime registered and the clones are rendered on CPU anyway.

| Piece | Value |
|-------|-------|
| Unit | `tts-api.service` (user level, enabled, port `8001`) |
| Venv | `~/ov_api/venv-omnivoice` (do not touch `~/ov_api/.venv`) |
| Config | `~/ov_api/.env` (read by pydantic-settings from the working dir) |
| News-engine | `TTS_API_BASE_URL` and `NARRATION_API_BASE_URL` → `http://host.docker.internal:8001/api/v1` |

```bash
systemctl --user status tts-api.service
curl -s http://localhost:8001/api/v1/health
docker compose exec -T api python -c "import httpx; print(httpx.get('http://host.docker.internal:8001/api/v1/health').json())"
```

### 10.1 Engine: `routed` → `pocket_tts`

The cloned voices carry `engine='pocket_tts'` in the gateway's database
(MichaelScott, Dwight), and the API refuses a clone whose tag does not match
the active engine. So `.env` routes by language instead of pinning one engine:

```bash
TTS_ENGINE=routed
TTS_ENGINES={"es":"pocket_tts","en":"pocket_tts","fr":"pocket_tts","ca":"edgetts","_default":"pocket_tts"}
PORT=8001
```

`routed` initializes every engine named in the map, so `edge-tts` has to be
installed too. It also means the 2.1 GB OmniVoice model stays unloaded while
narration runs, leaving the GPU to Ollama.

One-time install of the venv:

```bash
cd ~/ov_api
python3 -m venv venv-omnivoice
venv-omnivoice/bin/pip install torch==2.11.0+cu130 torchaudio==2.11.0+cu130 \
  --index-url https://download.pytorch.org/whl/cu130
venv-omnivoice/bin/pip install 'git+https://github.com/k2-fsa/OmniVoice.git' pocket-tts edge-tts
venv-omnivoice/bin/pip install fastapi 'uvicorn[standard]' pydantic pydantic-settings \
  python-multipart loguru soundfile pydub lameenc sqlalchemy alembic aiosqlite \
  structlog prometheus-fastapi-instrumentator slowapi httpx numpy
```

`torchaudio` tops out at 2.11 on the cu130 index, so torch and torchaudio have
to be pinned to the same 2.11.0 pair. The project itself pins Python `<3.13`
and the box only has 3.13, so the app is **not** installed as a package: run it
from the working directory, where pydantic-settings picks up `.env`.

### 10.2 Verifying a clone by hand

```bash
curl -s http://localhost:8001/api/v1/voices/cloned          # the clone ids
curl -s -X POST http://localhost:8001/api/v1/tts \
  -H 'Content-Type: application/json' \
  -d '{"text":"Prueba de audio.","voice_id":"3e47fbcc-53a8-4551-be91-48f93d62a19f","language":"en"}' \
  -o /tmp/clone.wav -w '%{http_code} %{size_download}\n'
file /tmp/clone.wav     # RIFF (little-endian) data, WAVE audio
```

A `404 voice_not_found (stock)` here means the engine cannot serve that clone:
either the voice is tagged for another engine or the cast is wrong. Stock ids
(`en-us-male`, `es-mx-male`, ...) live in `/api/v1/voices/stock`.

### 10.3 Rollback

```bash
systemctl --user stop tts-api.service
```

`NarrationTTSService` degrades instead of failing: the digest still completes
and records the narration as `error`. To move the clones back onto the
`omnivoice` engine instead, re-clone them there so their `engine` tag changes,
then set `TTS_ENGINE=omnivoice` and drop `TTS_ENGINES`.

### 10.4 Per-collection engine, the engine lease and the ETA (Phase 8)

Since Phase 8 the digest does not have to use edge: each collection picks its
own engine in `collections.toml`, and every synthesis in the stack goes through
one priority lease so a book render and a daily digest never hit the gateway at
the same time.

**Engine per collection**

```toml
[[collections]]
name = "ai"
tts_backend = "omni_voice"   # edge | omni_voice | qwen3_tts | zonos2
voice = "es-mx-male"         # id in THAT engine's vocabulary
```

`/ui → Colecciones` shows the voice list of whichever engine you select, so you
never type an Edge ShortName into a field the gateway will reject. Global floor
in `.env`:

```bash
TTS_DEFAULT_BACKEND=edge
TTS_FALLBACK_TO_EDGE=false     # never silently swap a clone for a stock voice
TTS_BACKEND_TIMEOUT_SECONDS=900  # POST /api/admin/tts uses 120s only for edge
```

The gateway backends store `.wav`, edge stores `.mp3`; the M4A transcode, the
Whisper check and every delivery are format-agnostic, so nothing downstream
changes.

**The lease** (`tts_leases` + `tts_lease_waiters`, migration `011`)

```bash
# Is anything holding the engine right now, and until when?
sqlite3 data/news.db 'SELECT owner, priority, expires_at FROM tts_leases;'
# Who is queued for it (highest priority first)?
sqlite3 data/news.db 'SELECT owner, priority, requested_at FROM tts_lease_waiters ORDER BY priority DESC;'
```

A document render shows `PAUSED` / «en pausa (digest)» in `/ui/tools` while it
has let go, and `GENERATING` again once the digest is done — that is the feature
working, not a stall. If a worker is killed mid-render the lease expires after
`TTS_LEASE_TTL_SECONDS` (300 s) and the next run proceeds on its own; the chunk
cache makes that retry cheap. `TTS_LEASE_ENABLED=false` restores the old
behaviour with no other change.

**Calibrating the ETA** (`/api/admin/documents/tts/{id}` → `estimate_wall_seconds`)

The forecast is only as good as its rate. The measured truth is already in the
DB for every finished job:

```bash
sqlite3 data/news.db "SELECT run_id, elapsed_seconds FROM audio_jobs \
WHERE kind='doc_tts' AND status='FINISHED_OK';"
```

Then `cps = (document chars still uncached) / (elapsed_seconds − 2)` (the `2` is
`TTS_JOIN_OVERHEAD_SECONDS`) and set `TTS_EDGE_CHARS_PER_SECOND` to it. While a
job runs the endpoint stops trusting the model and reports `eta_seconds`
extrapolated from the chunks already finished, so a wrong rate only shows up in
the first seconds of a run.

---

## 11. LLM verification (Phase 7.13)

Cada digest generado se verifica contra sus propias fuentes **antes** de
entregar nada: ni texto, ni audio, ni narración. Si la verificación falla, el
run queda en `verification_failed` en `/ui` y el workflow termina en error; la
fila de `digest_runs` conserva el texto para auditar qué se rechazó.

### 11.1 Activation (opt-in per collection)

Verification does nothing until a collection opts in:

```toml
[[collections]]
name = "ai"
feed_urls = ["https://elpais.com/rss"]
llm_verify = true
# optional per-collection overrides (empty = global settings)
llm_verify_threshold = 0.9
llm_verify_backend = "local"   # local | modal | api
```

The global kill switch `LLM_VERIFY_ENABLED=false` turns it off for every
collection at once. Defaults ship with verification **on globally but off per
collection**, so upgrading changes no existing digest.

### 11.2 What is checked

1. **Groundedness** (free, no LLM): every figure of the digest — currency,
   percentages, dates and bare numbers — must exist in a source article. Spelling
   variants are matched (`1.200` = `1200`, `1,2%` = `1.2%`). Markdown structure
   and product versions (`Llama 3.3`, `GPT-6`) are ignored so they cannot fail a
   digest. Threshold: `LLM_VERIFY_GROUNDEDNESS_THRESHOLD` (default `1.0`).
2. **Faithfulness** (LLM judge): the digest is split into atomic claims and each
   is labelled `supported | contradicted | unsupported` with a verbatim quote.
   Score = supported / total, compared against `LLM_VERIFY_THRESHOLD`
   (default `0.9`). A digest with fewer than `LLM_VERIFY_MIN_CLAIMS` claims is
   scored on groundedness alone.

Both layers are written to the `verifications` table (one row per layer), so a
failure can be audited later:

```bash
sqlite3 data/news.db 'SELECT method, verdict, score, threshold FROM verifications ORDER BY id DESC LIMIT 5;'
```

### 11.3 Judge backends

| Backend | Endpoint | Model default | Cost |
|---|---|---|---|
| `local` (default) | `LLM_API_BASE` | `LLM_MODEL` | free |
| `api` | `LLM_VERIFY_API_BASE` (empty = `LLM_API_BASE`) | provider default | paid |
| `modal` | Modal GPU | `MODAL_JUDGE_MODEL` (Qwen/Qwen3-8B) | per-use |

`api` keeps digest generation on your local model while only the judge runs on a
hosted provider (Groq, OpenRouter, ...): set `LLM_VERIFY_API_BASE`,
`LLM_VERIFY_API_KEY` and `LLM_VERIFY_BACKEND=api`. The Modal backend requires
`MODAL_ENABLED=true` plus Modal credentials; it is inert otherwise. The key is
never written to logs or the `verifications` rows.

### 11.4 Calibration

`LLM_VERIFY_THRESHOLD=0.9` is a starting point, not a measured value. Before
tightening it for a real collection:

1. Run 10-15 known-good digests through the pipeline with `llm_verify = true`
   (a failing run never delivers, so test against a throwaway collection).
2. Read `SELECT score FROM verifications WHERE method='faithfulness'` and check
   that every accepted digest sits above the threshold with margin.
3. Raise the threshold to the lowest accepted score minus ~0.05; lower it if
   good digests are rejected.

Groundedness stays at `1.0`: there is no acceptable level of a fabricated figure.
A false positive there means a number was misread (usually a model version or a
date fragment) — fix the extractor in
`src/api/services/verification.py::extract_figures` instead of lowering the bar.

### 11.5 Source reliability

Feeds can be trusted to different degrees; an untrusted source is excluded
**before clustering**, so it never reaches the prompt at all:

```toml
feed_urls = [
  "https://elpais.com/rss",                       # 1.0 (SOURCE_RELIABILITY_DEFAULT)
  { url = "https://news.google.com/rss",          # aggregator: score the feed,
    reliability = 0.6,                            # map its real publishers
    hosts = ["bbc.co.uk", "cnn.com"] },
]
```

- A feed whose reliability is below `MIN_SOURCE_RELIABILITY` (default `0.5`) is
  dropped in `pipeline_config_for` before the run starts.
- Article reliability is looked up by the host of the article's own `link`, not
  by the feed URL, which is why aggregators need `hosts`.
- Reliability also weights the per-source faithfulness breakdown shown in the
  report (`details.sources`).

### 11.6 Audit endpoints

```bash
# ledger of one run (needs the run's workflow_id)
curl -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  http://localhost:8000/api/admin/runs/<run_id>/verifications
```

The GUI shows `verification_failed` runs in the run list with their digest
preview.

### 11.7 Rollback

Nothing runs without `llm_verify = true`, so the phase ships inert. To disable
everything at once:

```bash
# .env
LLM_VERIFY_ENABLED=false
```

then restart the worker and re-register the schedules, because `verify` is
resolved into the schedule payload when `schedule.py` builds it:

```bash
docker compose up -d --force-recreate worker
docker compose exec worker python -m src.workers.schedule
```

Manual runs (`trigger.py`, `POST /admin/collections/{name}/run`) resolve the
config at request time, so they pick the change up after a restart of the API
and the worker without re-registering anything.

---

## 12. URL summary (Phase 7.10)

Resumir un solo artículo a partir de su URL, con los mismos prompts, destinos
y verificación que el digest diario.

### 12.1 Usage

```bash
curl -X POST http://localhost:8000/api/summarize/url \
  -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://elpais.com/economia/2026/...", "collection": "tech"}'
```

Respuesta **202**:

```json
{"status": "ok", "workflow_id": "url-summary-tech-20261003120000-abcd1234",
 "collection": "tech", "url": "https://elpais.com/..."}
```

Parámetros del cuerpo:

| Campo | Por defecto | Efecto |
|---|---|---|
| `url` | — (obligatorio) | Solo `http`/`https`; cualquier otro esquema da 422 |
| `collection` | `default` | Colección cuyo prompt y destinos se usan |
| `store` | `false` | `true` guarda el artículo en `articles` (URL normalizada) |
| `deliver` | `true` | `false` genera y registra sin entregar a ningún canal |

Errores: `401` sin token, `403` con `URL_SUMMARY_ENABLED=false`, `404` si la
colección no existe (sin arrancar nada), `422` URL inválida, `502` si Temporal
no acepta el workflow.

El estado del run es el de siempre: `GET /api/admin/runs` y la tabla del GUI en
`/ui` (estados `ok`, `failed`, `verification_failed` con el texto del digest
para auditar).

### 12.2 Pipeline

`UrlSummarizeWorkflow` = `fetch → (store) → generate → verify → deliver → record`:

1. **fetch** — `httpx` descarga la página (`URL_FETCH_TIMEOUT`,
   `URL_FETCH_MAX_BYTES`, solo http/https) y `trafilatura` extrae el cuerpo en
   un hilo aparte (CPU-bound). Páginas sin cuerpo legible (muro de pago, apps
   JS) fallan de forma permanente: se registra el run y no se entrega nada.
2. **store** — solo con `store = true`. La URL se guarda normalizada (mismas
   claves de tracking eliminadas que usa el dedup del RSS), de modo que una
   ingesta posterior de la misma historia no la duplique.
3. **generate** — la activity `generate_digest_activity` de siempre con el
   prompt, el idioma y los counts de la colección.
4. **verify** — si la colección tiene `llm_verify = true` pasa el mismo gate de
   la Fase 7.13: no hay forma de esquivar la verificación usando este endpoint.
5. **deliver** — telegram/email/whatsapp según `delivery_channels`. Un fallo de
   canal se registra y no tumba el run (el texto ya existe).
6. **record** — la fila en `digest_runs` se escribe siempre, también en fallo.

### 12.3 Settings

```bash
URL_SUMMARY_ENABLED=true   # kill switch del endpoint
URL_FETCH_TIMEOUT=30       # segundos por descarga
URL_FETCH_MAX_BYTES=5000000
URL_SUMMARY_MAX_CHARS=12000  # cuerpo que llega al prompt (más se trunca)
```

### 12.4 Notes

- El endpoint está tras el mismo middleware que `/api/admin/*`: quien lo usa
  ya administra el servidor, y la descarga se hace **desde** el servidor (no
  desde el cliente). Mantener `ADMIN_USERNAME`/`ADMIN_API_TOKEN` configurado.
- El resumen es solo texto: no genera audio (TTS/narración siguen siendo cosa
  del digest diario).
- Requiere `trafilatura` (ya en `pyproject.toml`); tras cambiar dependencias:
  `pip install -e .` en el venv y `docker compose build api worker`.

### 12.5 Rollback

```bash
# .env
URL_SUMMARY_ENABLED=false
```

y reinicia la API (`docker compose up -d --force-recreate api`). El endpoint
responde 403 sin tocar el código; el resto del sistema no se ve afectado.

---

## 13. Document TTS (Phase 7.11)

Convierte un documento largo (PDF, EPUB, TXT, MD) en **un solo MP3** y lo deja
disponible como enlace firmado. Nada se envía a Telegram/email: la entrega es
la URL, pensada para abrirla desde el móvil por la LAN o por Tailscale.

### 13.1 One-time setup

```bash
# 1. Secret for the shareable links (required; without it the submit 503s)
echo "DOC_TTS_LINK_SECRET=$(openssl rand -hex 32)" >> .env

# 2. Rebuild and start the dedicated worker (own queue: a 300-page render can
#    never occupy the digest worker's slots)
docker compose up -d --build api worker doc-tts-worker
```

The worker entry point is `python -m src.workers.run_doc_tts_worker`
(`--queue` overrides `DOC_TTS_TASK_QUEUE`). It runs in the **default**
namespace, same as the digest: the API schedules there and an activity always
executes in the namespace of its workflow.

### 13.2 Usage

```bash
# Submit (multipart; voice/language/title are optional form fields)
curl -X POST http://localhost:8000/api/admin/documents/tts \
  -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  -F "file=@apuntes.epub" -F "title=Clase de espanol" -F "language=es"
# 202 {"workflow_id": "doc-tts-ab12cd34ef56", "status_url": "...", ...}

# Poll until job_status is FINISHED_OK
curl -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  http://localhost:8000/api/admin/documents/tts/doc-tts-ab12cd34ef56
# {"job_status": "GENERATING", "char_count": 0, "audio_url": null, ...}
# {"job_status": "FINISHED_OK", "char_count": 432100, "chunk_count": 108,
#  "duration_estimate": 28806.67, "audio_url": "/api/audio/doc/...?exp=...&sig=..."}

# Open the link (NO admin token: the signature is the authorization)
curl -OJ "http://192.168.5.6:8000/api/audio/doc/doc-tts-ab12cd34ef56?exp=...&sig=..."
```

Estados del job (`audio_jobs.kind='doc_tts'`): `STARTING` (aceptado) →
`GENERATING` (extracción + síntesis) → `FINISHED_OK` / `FINISHED_ERROR` con el
motivo en `error`. Los metadatos (título, `char_count`, `chunk_count`,
duración) viven en `data/doc_tts/meta/<id>.json`.

### 13.3 Pipeline

`DocumentTtsWorkflow` = `extract → synthesize`, en la cola
`DOC_TTS_TASK_QUEUE`:

1. **extract** — `pypdf`/`ebooklib` extraen el texto en orden de lectura
   (sin la nav del EPUB; un PDF escaneado sin texto falla con explicación) y lo
   escriben en `data/doc_tts/text/<id>.txt` (los payloads de Temporal no viajan
   con el libro). Límite: `DOC_TTS_MAX_SOURCE_CHARS`.
2. **synthesize** — troceado en párrafos→frases→palabras
   (`DOC_TTS_CHUNK_CHARS`), `DOC_TTS_CONCURRENCY` trozos en paralelo con
   reintentos individuales, unión en orden en un MP3. Cada trozo se cachea por
   hash y el resultado final por (voz, idioma, texto): un reinicio retoma donde
   estaba. La activity manda un heartbeat por trozo.

### 13.4 Settings

```bash
DOC_TTS_ENABLED=true                 # kill switch del endpoint
DOC_TTS_TASK_QUEUE=news-engine-doc-tts-queue
DOC_TTS_MAX_FILE_BYTES=20000000      # 413 si se supera
DOC_TTS_MAX_SOURCE_CHARS=600000      # ~300 páginas
DOC_TTS_CHUNK_CHARS=4000
DOC_TTS_CONCURRENCY=4
DOC_TTS_DIR=data/doc_tts
DOC_TTS_LINK_SECRET=                 # obligatorio; ver 13.1
DOC_TTS_LINK_TTL_SECONDS=604800      # caducidad de los enlaces
```

### 13.5 Notes

- Auth doble: el **submit** y el **estado** van tras el middleware de admin
  (`/api/admin/*`); el **enlace de audio** es el único endpoint público del
  proyecto y su autorización es el HMAC (`exp` + `sig` sobre
  `"<id>:<exp>"`). Enlaces manipulados, caducados o de otro job dan 403, y el
  fichero que se sirve está confinado a `DOC_TTS_DIR/audio`.
- Sin `DOC_TTS_LINK_SECRET` el submit responde 503: un trabajo sin enlace
  servible no tiene sentido.
- La voz por defecto es la del digest (`TTS_VOICE`/`TTS_LANGUAGE`); se puede
  sobreescribir por llamada con los campos `voice`/`language`.
- Nuevas dependencias: `pypdf`, `ebooklib`, `python-multipart`. Tras un
  `git pull`: `pip install -e .` en el venv y `docker compose build api worker
  doc-tts-worker`.
- Limpieza manual: `rm -rf data/doc_tts/{uploads,text}` es seguro; borra
  también `data/doc_tts/audio` y `meta` si ya no necesitas los enlaces.

### 13.6 Rollback

```bash
# .env
DOC_TTS_ENABLED=false
```

y reinicia la API (`docker compose up -d --force-recreate api`): el endpoint
responde 403 sin tocar código. Para retirar la cola por completo:
`docker compose stop doc-tts-worker` (los jobs en vuelo quedan `GENERATING`;
borra sus filas en `audio_jobs` si no te importan).

---

## 14. Language teacher (Phase 7.12)

Subes unos apuntes → el LLM genera la clase (Markdown para leer + guion
hablado) → se graba con el cast que tú indiques (voces clonadas) → llega por
los canales de la colección: el **texto siempre como adjunto** y el **audio
solo si dura ≤ `TEACHER_MAX_AUDIO_SECONDS`**; si dura más, el texto lleva un
enlace firmado en su lugar.

### 14.1 One-time setup

```bash
# The signed audio links reuse the secret from section 13; generate it once:
grep -q DOC_TTS_LINK_SECRET .env || echo "DOC_TTS_LINK_SECRET=$(openssl rand -hex 32)" >> .env

docker compose up -d --build api worker teacher-worker
```

The teacher worker runs `python -m src.workers.run_teacher_worker`
(`--queue` overrides `TEACHER_TASK_QUEUE`), in the **default** namespace. Its
queue only executes the LLM call and the render; deliveries are addressed by
name on the digest queue, so a slow class never delays a daily digest.

### 14.2 Usage

```bash
# 1) Pick your cast (cloned voices live in the catalog)
curl -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  http://localhost:8000/api/admin/narration/voices

# 2) Submit the notes (spec = NarrationSpec JSON with your roles)
curl -X POST http://localhost:8000/api/admin/lessons \
  -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  -F "file=@apuntes_subjuntivo.md" \
  -F "mode=dual" \
  -F "target_language=en" -F "native_language=es" \
  -F "collection=tech" -F "title=Subjuntivo en 10 minutos" \
  -F 'spec={"language":"en","roles":[
        {"name":"profe_es","voice_id":"<clon-es>","language":"es"},
        {"name":"profe_en","voice_id":"<clon-en>","language":"en"}]}'
# 202 {"workflow_id": "lesson-ab12cd34ef56", "mode": "dual", "status_url": "..."}

# 3) Poll until FINISHED_OK
curl -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  http://localhost:8000/api/admin/lessons/lesson-ab12cd34ef56
# {"job_status": "FINISHED_OK", "duration_estimate": 180.0,
#  "audio_url": "/api/audio/lesson/lesson-ab12cd34ef56?exp=...&sig=...", ...}
```

Si omites `spec`, pásale `voice=<id>` (o nada) y se construye un cast de una
voz con `TTS_VOICE`. En `dual` sin `spec` hacen falta las dos voces:
`voice=<nativa>` y `voice2=<objetivo>` (422 si falta cualquiera). Modos:

| Modo | Voces | Qué hace el LLM |
|---|---|---|
| `mixed` | 1+ | Explica en la lengua nativa e intercala ejemplos en la objetivo |
| `dual` | **2+** (requisito, si no 422) | Primera voz explica en nativa, segunda enseña en la objetivo |
| `native` | 1+ | Todo en la lengua nativa; la objetivo aparece citada como material |

### 14.3 Pipeline

`LessonWorkflow` = `generate → render → deliver`, en `TEACHER_TASK_QUEUE`:

1. **generate** — `generate_lesson_activity` valida las notas (`.md`/`.txt`),
   construye el prompt con el modo y el cast, llama al LLM (LiteLLM,
   temperatura 0.7), parsea el JSON tolerante a vallas de código y guarda
   `data/lessons/text/<id>.md` (adjunto) + `<id>.script.json` (guion). Solo
   viajan paths por Temporal. Un fallo de validación es permanente
   (`FINISHED_ERROR` sin reintentos); un fallo de red del LLM reintenta.
2. **render** — `render_lesson_activity` sintetiza con
   `NarrationTTSService` (las MISMAS voces clonadas de la narración) y cierra
   la fila de `audio_jobs` (`kind='lesson'`). Aquí se construye también la
   URL firmada (las activities sí pueden leer settings; los workflows no).
3. **deliver** — en la cola del digest: email con el `.md` adjunto
   (`attachments=[...]` en `deliver_email_activity`) y Telegram con
   `sendDocument`. Si `duration ≤ TEACHER_MAX_AUDIO_SECONDS` se adjunta/manda
   el audio; si no, el cuerpo y el caption llevan la URL firmada. Un fallo de
   canal se registra y no tumba la lección.

### 14.4 Settings

```bash
TEACHER_ENABLED=true                  # kill switch del endpoint
TEACHER_TASK_QUEUE=news-engine-teacher-queue
TEACHER_MAX_FILE_BYTES=5000000        # 413 si se supera
TEACHER_MAX_NOTES_CHARS=60000
TEACHER_MAX_AUDIO_SECONDS=300         # umbral audio adjunto vs URL
TEACHER_DIR=data/lessons
TEACHER_LINK_TTL_SECONDS=604800
```

### 14.5 Notes

- Auth: los dos endpoints están bajo `/api/admin/*` (middleware de admin); el
  enlace de audio es público y se valida con HMAC (mismo secreto que los
  documentos de la §13). Sin `DOC_TTS_LINK_SECRET` el submit da 503.
- Los canales salen de la colección (`delivery_channels`, `chat_id`,
  `email_recipients`); una colección desconocida cae a Telegram global.
  **WhatsApp no está soportado para lecciones** (no hay envío de documentos).
- El estado vive en `audio_jobs(kind='lesson')` + `data/lessons/meta/<id>.json`.
- Nuevas dependencias en esta fase: ninguna (multipart, email y LLM ya estaban).

### 14.6 Rollback

```bash
# .env
TEACHER_ENABLED=false
```

y reinicia la API (`docker compose up -d --force-recreate api`): 403 sin tocar
código. Para retirar la cola: `docker compose stop teacher-worker` (los jobs
en vuelo quedan `GENERATING`; borra sus filas en `audio_jobs` si no te importan).

---

## 15. Audio verification with Whisper (Phase 7.14)

Antes de entregar un audio, se transcribe con `faster-whisper` (CPU) y se
compara con el texto que lo generó mediante **WER** (`jiwer`, word error rate:
cuanto más bajo, mejor). Si el WER supera el umbral, **se retiene solo el
audio** — el texto del digest ya entregado no se toca (es primario) — y el
veredicto queda en el ledger `verifications`.

- **TTS del digest**: el texto esperado es exactamente lo que recibió el motor
  (`speech_text_for_digest`: truncado + Markdown → habla).
- **Narración**: el texto esperado es el script en orden (+ divulgación de IA
  audible si la spec la pide).
- Opt-in por colección (`audio_verify = true`); solo se verifica si la
  colección entregaría audio (`deliver_audio` / `narration_deliver_audio`).

### 15.1 One-time setup

```bash
docker compose up -d --build api worker narration-worker
```

`faster-whisper`, `jiwer`, `ctranslate2` y `av` van en la imagen. **Primera
verificación**: el modelo se descarga de HuggingFace (~150 MB para `base`) a
`AUDIO_VERIFY_MODEL_DIR=data/whisper` (volumen compartido, se descarga una sola
vez y sobrevive a los rebuilds). Sin salida a internet, la verificación responde
`verdict=error` y el audio se retiene (fail-closed).

### 15.2 Enable

```toml
[[collections]]
name = "tech"
audio_verify = true            # opt-in de la colección
audio_verify_threshold = 0.30  # opcional; vacío = AUDIO_VERIFY_WER_THRESHOLD
```

Kill switch global: `AUDIO_VERIFY_ENABLED=false` (apaga todas las colecciones).

### 15.3 Qué se mide

El audio se decodifica a 16 kHz mono con ffmpeg, se trocea en bloques de
`AUDIO_VERIFY_BLOCK_SECONDS` (cada bloque se transcribe con
`condition_on_previous_text=False` para que un bucle de repetición no contamine
al resto) y se comparan los textos **normalizados** (minúsculas, sin puntuación
ni tags de emoción: `3,5%` y `3.5%` tokenizan igual). El resultado incluye
`wer`, `similarity = 1 - WER`, el transcript y los transcripts por bloque.

### 15.4 Cuando falla (gate solo de audio)

| Caso | Qué pasa |
|---|---|
| `wer > umbral` | Veredicto `invalid`, audio **no** entregado; el texto sigue saliendo |
| ASR imposible (sin muestras, modelo caído) | Veredicto `error`, audio retenido (fail-closed) |
| Sin opt-in | El gate no se ejecuta; el audio fluye como siempre |

Queda registrado en `delivery_status` con `channel: "audio_verification"`
(TTS) o `"narration_audio_verification"` (narración), y una fila por capa en el
ledger:

```sql
SELECT target, method, verdict, score, threshold, passed, created_at
FROM verifications WHERE method = 'asr_wer' ORDER BY id DESC LIMIT 10;
```

### 15.5 Settings

```bash
AUDIO_VERIFY_ENABLED=true       # kill switch global
AUDIO_VERIFY_MODEL=base         # tiny/base/small/medium/large-v3
AUDIO_VERIFY_DEVICE=cpu
AUDIO_VERIFY_COMPUTE_TYPE=int8
AUDIO_VERIFY_MODEL_DIR=data/whisper
AUDIO_VERIFY_WER_THRESHOLD=0.30 # techo de WER (0 = estricto, 1 = inútil)
AUDIO_VERIFY_BLOCK_SECONDS=60
AUDIO_VERIFY_LANGUAGE=          # p.ej. 'es'; vacío = auto-detección
```

### 15.6 Calibration

1. Con `audio_verify = true` en una colección de prueba, deja pasar 10-15
   digests/narraciones conocidos.
2. Lee los valores: `SELECT score FROM verifications WHERE method='asr_wer' AND passed=1`
   y coloca el umbral **por encima del peor aceptado** con margen (~0.05).
3. Ten en cuenta dos sesgos del ASR observados en el smoke test: números mal
   leídos (`2027` → `2017`, +0.08 de WER en una frase de 12 palabras) y la
   música de fondo de la narración, que infla el WER: para narración con music
   bed sube el modelo (`small`) o el umbral, no bajes la barra del TTS.
4. El umbral solo afecta a colecciones con `audio_verify = true`, así que
   calibrar es reversible y no toca al resto.

### 15.7 Rollback

```bash
# .env
AUDIO_VERIFY_ENABLED=false
```

o `audio_verify = false` por colección; reinicia los workers
(`docker compose up -d --build worker narration-worker`). El modelo ya descargado
puedes borrarlo con `rm -rf data/whisper` cuando no lo necesites.

---

## 16. GUI de operación (herramientas, cast, verificaciones y ajustes)

La GUI (`/ui`) cubre ahora todas las fases 7.10-7.14 sin tocar la API REST.

### 16.1 Herramientas (`/ui/tools`)

Cuatro pestañas con polling htmx cada 10 s (`/ui/fragments/tool_jobs?kind=...`):

| Pestaña | Acción | Estado |
|---|---|---|
| Resumen de URL | `POST /ui/tools/url` (url, colección, store, deliver) | últimos `url-summary-*` en `digest_runs` |
| TTS de documentos | `POST /ui/tools/doc-tts` (PDF/EPUB/TXT/MD) | jobs `kind='doc_tts'` + enlace firmado |
| Clase de idiomas | `POST /ui/tools/lessons` (apuntes, modo, idiomas, cast) | jobs `kind='lesson'` + enlace/texto |
| Podcast | `POST /ui/tools/podcast` (colección) | jobs `kind='narration'` + enlace firmado |

- Los submits delegan en los **mismos servicios** que la REST
  (`submit_document`, `submit_lesson`, `start_narration_render`): una misma
  validación para ambos caminos, traducida a flash en la GUI y a código HTTP
  en la API.
- El podcast se genera a partir del **último digesto** de la colección (el LLM
  escribe el guion) y usa su `NarrationSpec`/estrategia; si no hay ejecuciones
  o falta el spec, la GUI lo explica sin lanzar nada.
- Enlaces firmados: `/api/audio/doc/...`, `/api/audio/lesson/...` y
  `/api/audio/narration/...` (nuevo) — mismos secretos y TTL, sin token.

### 16.2 Cast de narración (`/ui/collections`)

- El textarea JSON se sustituyó por un **editor de cast**: filas rol
  (nombre, voz con `datalist` del catálogo, idioma, velocidad, emoción) + campos
  del spec (título, idioma, pausas, velocidad, emoción, backend, divulgación de
  IA). Las filas vacías se ignoran; nombre sin voz (o al revés) rechaza el
  guardado con mensaje.
- El **JSON avanzado** sigue disponible en un `<details>`: se usa solo si el
  cast de arriba está vacío, y los campos que el editor no expone (`music`,
  `max_chars_per_request`...) se **conservan** del spec existente.
- Botón **«Generar podcast ahora»** por colección →
  `POST /ui/collections/{name}/narrate` → estado en la pestaña Podcast de
  Herramientas.
- Corregidos dos bugs preexistentes de este flujo: `narration_spec|tojson`
  reventaba la página con cualquier spec (500), y el serializador TOML
  descartaba la lista `roles` al guardar (**borraba el cast**). `collections.toml`
  ya no se puede corromper desde la GUI.

### 16.3 Verificaciones en Ejecuciones

- El detalle de cualquier ejecución muestra la tabla de
  `verifications` (`target`, `method`, veredicto, score, umbral, apto) con los
  detalles JSON plegables — incluye `groundedness`/`faithfulness` (7.13) y
  `asr_wer` (7.14, con la nota «menor es mejor»).
- Los runs bloqueados por verificación llevan el chip ámbar
  `verification_failed` (ya etiquetado en la lista).

### 16.4 Editor de configuración (`/ui/settings`)

- **Esquema derivado de `Settings`**: las ~159 variables, agrupadas por sección
  (LLM, verificaciones por fase, documentos, profesor, entregas, temporal...),
  con tipo, rango y default. Añadir un setting en `config.py` lo añade solo.
- **Validación de todo-o-nada**: se construye `Settings` con los valores
  enviados; si algo no valida (tipo, rango, JSON, `LLM_VERIFY_BACKEND`...),
  **no se escribe nada** y la página vuelve con el error por campo y el valor
  tecleado para corregirlo.
- **Solo cambian las líneas modificadas**: comentarios, orden y
  `KEY=value  # nota` intactos; claves nuevas al final bajo un marcador;
  copia previa en `.env.bak`; escritura atómica.
- **Secretos** (🔒): su input va siempre vacío con placeholder
  «déjalo vacío para conservar»; jamás se reflejan en el HTML (ni siquiera el
  valor tecleado).
- **Reinicio obligatorio**: los settings viven cacheados en cada proceso
  (`get_settings()`), así que la página recuerda el comando:
  `docker compose restart api worker narration-worker doc-tts-worker teacher-worker voice-catalog-worker`.
- **Límites conocidos**:
  - Solo añade/actualiza: no borra claves del `.env`.
  - Las variables que Docker fuerza en `environment:` (p.ej.
    `TEMPORAL_HOST=temporal:7233`) las pisan el compose, no el `.env`; la
    página las muestra con su valor efectivo, pero escribirlas no las cambia en
    los contenedores hasta retirarlas del compose.
  - Valores «válidos pero tontos» (p.ej. un límite enorme) pasan: la
    validación es de tipo y rango, no de criterio.

### 16.5 Rollback

La GUI no cambia datos de negocio: para volver a un `.env` anterior, restaura
`.env.bak` y reinicia. Para retirar el editor entero no hay flag: es UI
protegida por el middleware de admin (junto al resto de `/ui`).
