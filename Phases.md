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
- [x] Per-collection delivery channels (`delivery_channels`: telegram, email o
      ambos) con audio adjunto en email cuando corresponda

**Success criteria:** 3 independent Telegram digests at different times.

**Effort:** 6-8 hours.

---

## PHASE 5.1 - WhatsApp Delivery Channel (BlakePro Baileys gateway)

**Goal:** Deliver digests (text + audio M4A) on WhatsApp to multiple recipients
and/or a group, self-hosted via `blakegt/whatsapp-web-api-rest` (Baileys) in
docker-compose under the service name `there`.

**Scope:**
- [x] `there` gateway service in `docker-compose.yml` (internal network only, no
      published ports, session persisted in `data/there`, one-time QR pairing via
      SSH tunnel to `:8085`)
- [x] `WHATSAPP_*` config: API URL/token, E.164 `WHATSAPP_ALLOWLIST`
      (fail-closed closed address book), delay mode (off|natural|custom),
      typing indicator, per-recipient cool-down
- [x] `CollectionConfig.whatsapp_recipients` (E.164) + `whatsapp_group_id`
      (→ `<id>@g.us`, exempt from allowlist); channel whitelist gains "whatsapp"
- [x] `WhatsAppService` (fail-closed, JID `@c.us`/`@g.us` conversion, chunking,
      base64 audio gate, OpenClaw-style humanDelay + per-recipient cool-down,
      typing indicator via `/simulate`)
- [x] `deliver_whatsapp_message_activity` / `deliver_whatsapp_audio_activity`
      (non-retryable on unconfigured/not-allowlisted, network retryable)
- [x] `DigestPipelineWorkflow` WhatsApp delivery blocks (text after email, audio
      after Telegram audio; a failure never aborts the run)
- [x] Unit tests (service + activities) and workflow stub tests
- [x] Deploy: validate gateway image + `POST /message` + `POST /simulate` contract
      against the running container, pair once via QR (GET / shows the QR)

**Success criteria:** A digest configured with `delivery_channels = ["whatsapp"]`
reaches every allowlisted recipient (and the group) as text + audio with human-like
pacing; numbers outside the allowlist are never contacted.

**Effort:** 6-8 hours.

---

## PHASE 5.2 - Audio file naming + News-selection logic (admin)

**Goal:** (1) name the generated audio `news_<collection>_<yymmdd>.<ext>` and
(2) send the LLM's news-selection rationale to an optional admin destination,
separate from the digest message.

**Scope (paso 1 - nombre de audio):**
- [x] `TTSIn.collection`; `TTSService` genera `news_<collection>_<yymmdd>.mp3`
      en `data/tts_cache/` (colección saneada `[a-z0-9_-]`, fecha UTC `%y%m%d`)
- [x] `generate_tts_activity` propaga `collection`; compresión conserva la base
      (`.m4a`)
- [x] Tests de naming/cache/actividad actualizados (patrón `news_<col>_<yymmdd>`)

**Scope (paso 2 - lógica de selección):**
- [x] `CollectionConfig.logic_delivery` (opcional; ausente => el LLM NO la genera)
- [x] Generación LLM en llamada separada (`generate_logic_activity`)
- [x] `deliver_logic_activity` (telegram/email/whatsapp, mensaje aparte)
- [x] Wiring en `PipelineConfig` + `pipeline_config_for` + monitor de schedules
- [x] Roundtrip admin API (`CollectionIn`, serializador TOML)
- [x] Tests unit/integración (284 passing, ruff limpio)
- [x] Deploy + configuración de `logic_delivery` de health (email a jordias@msn.com)

**Effort:** 4-6 hours.

---

## PHASE 5.3 - System Monitoring (SystemMonitorWorkflow)

**Goal:** un monitor de salud del sistema: TTS vivo, colecciones validadas,
aviso al actualizar una colección, procesos/servicios, disco del servidor y del
servidor TTS, e indicar runs lanzados/terminados. Monitorizaciones fáciles de
añadir (registro declarativo).

**Scope:**
- [x] `CheckResult`/`MonitorReport`/`MonitorRunConfig` (Pydantic v2)
- [x] Registro declarativo `MONITOR_CHECKS` (añadir check = función + registro)
- [x] Checks: docker_containers (socket ro), edge_tts, tts_server_disk,
      services (api/temporal/there), collections (file+feeds+schedules),
      digest_runs (36h cutoff), disk_local (disco host + tamaño de tts_cache)
- [x] `DockerService` vía UNIX socket (`/var/run/docker.sock` ro en el worker)
- [x] `MonitorReporter`: persistencia SQLite + JSONL, transiciones
      (alerta solo al pasar a error/warning), resumen diario UTC 1×,
      avisos de runs completados (`monitor:last_run_ts`)
- [x] Migración `007_monitor.sql` (tabla `monitor_events` + retención)
- [x] `SystemMonitorWorkflow` + schedule `system-monitor` (intervalo configurable)
- [x] Alerta de colecciones añadidas/removidas en `CollectionsMonitorWorkflow`
      (diff antes del resync, via `collections_changes_activity`)
- [x] Config `MONITOR_*` en `.env.example` + `.env`
- [x] Tests unit/integración (348 passing, ruff limpio)
- [x] Deploy: montar socket docker en el worker, resync de schedules
      (validado: `docker_containers` ok, monitor_events 56 filas, marcar
      schedules vía `python -m src.workers.schedule`)
- [x] Endpoint `GET /disk` en el servidor TTS (`tts_server_disk` ok,
      44.7% free via `debwk.elver-exponential.ts.net:8000/api/v1/disk`)

**Effort:** 6-8 hours.

---

## PHASE 5.4 - Hardening (P1 review fixes)

**Goal:** cerrar los defectos P1 detectados en la revisión previa a la Fase 7,
sin cambios de arquitectura.

**Scope:**
- [x] Round-trip de colecciones: `CollectionIn` incluye `whatsapp_recipients`,
      `whatsapp_group_id` y `logic_delivery` (con `extra="forbid"`), el
      serializador TOML cubre todos los campos de `CollectionConfig` y el GUI
      (crear/editar) expone WhatsApp + bloque de `logic_delivery`
- [x] Caché TTS por hash de contenido: sidecar `news_<col>_<yymmdd>.meta.json`
      con SHA-256 de (texto de habla, voz resuelta, idioma); escritura atómica
      del MP3/sidecar y transcodificación M4A solo si el origen es más nuevo
- [x] Endpoint TTS protegido: `POST /api/admin/tts` (admin token, kill switch
      `TTS_API_ENABLED`) y `POST /api/generate_tts` → 410 Gone (se elimina en
      la siguiente release); `TTSIn.markdown` con `max_length=20000`
- [x] Test con timestamps fijos (`check_digest_runs`) parametrizado con fechas
      relativas, para que no vuelva a pudrirse
- [x] Tests unit/integración + ruff limpio

**Effort:** 3-4 hours.

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

## PHASE 7 - Natural Narration (audio multi-speaker)

**Goal:** generar, desde el mismo digest, un piece de audio guionizado con varios
roles (presentador + invitados), con cama musical local, y entregarlo por los
mismos canales que el TTS actual. El dominio es genérico: no es "un podcast de
noticias", es "narración multi-hablante de un documento".

> **Nota de proceso:** la regla de cabecera (no empezar la fase N+1 hasta 3 días
> estables de la N) se salta aquí por indicación explícita del usuario. La Fase
> 5.4 (hardening) está mergeada y con la suite en verde como base.

**Decisiones de arquitectura (aprobadas):**
- Voces vía gateway TTS externo (`ov_api`) con Edge / PocketTTS / OmniVoice y
  fallback directo a Edge; nunca se llama al SDK de TTS desde routes o workflows
- Solo audio: "títulos por debajo" son titulares hablados, no rótulos visuales
- Biblioteca musical **local** (nada de descargas en runtime)
- Guion generado por LLM + edición/API opcional
- Activación por colección, con kill switch global
- Worker y task queue propios (`narration-worker`), misma imagen Docker

**Colisiones resueltas en 7.1 (migración 008):**
- `audio_jobs` gana `kind` (`tts` | `narration`): el `UNIQUE(run_id)` impedía que
  el TTS del digest y la narración coexistieran en la misma run
- `digest_runs` gana `narration_path` (`audio_path` queda reservado al TTS del
  digest); `record_run` usa `COALESCE` para que el digest no borre la ruta que
  escribió el worker de narración
- `narration_scripts` guarda el guion authored (JSON) por run
- `narration_*` es un namespace de config **propio**: `tts_api_base_url` no se
  reapunta (lo consume `VoicesService`)

**Scope:**
- [x] **7.1 Cimientos** — `src/api/models/narration.py` (`SpeakerRole`,
      `MusicBed`, `NarrationSpec`, `NarrationSegment`, `NarrationScript`) con
      bounds del contrato del gateway; settings `narration_*`; migración 008;
      `AudioJobsRepository.kind`; `RunsRepository.narration_path`;
      `NarrationRepository`; tests
- [ ] **7.2 Cliente del gateway + render** — health, catálogo de voces,
      `/tts`, `/conversations`; troceado cliente (el gateway no hace chunking);
      ensamblado WAV; música con ffmpeg; encode M4A; caché con sidecar
- [ ] **7.3 Workflow y cola propias** — `narration_activities.py`,
      `NarrationWorkflow`, `narration-worker` en su task queue, despacho desde
      `DigestPipelineWorkflow` con `ActivityOptions(task_queue=...)` y actividades
      por nombre
- [ ] **7.4 API y GUI** — `POST /api/admin/narration`,
      `GET /api/admin/narration/voices`, opt-in por colección en
      `collections.toml` + GUI, player de narración en el detalle de run
- [ ] **7.5 Guion LLM + ops** — prompts, activity de guion, servicio
      `narration-worker` en compose, `status.sh`, Deploy.md y Architecture.md

**Success criteria:** una colección opt-in produce un M4A de N minutos con ≥2
voces distintas, música de fondo, cacheado por hash y entregado por el canal
configurado, sin tocar el pipeline de audio actual.

**Effort:** 12-16 hours.

---

## ANTI-PATTERNS TO AVOID

- Do NOT add collections/multi-user before the single pipeline is stable
- Do NOT use Postgres for application state in Phase 1-4 (Temporal's own
  `temporal-postgres` is fine; news-engine state stays in SQLite)
- Do NOT build a web UI before Phase 5
- Do NOT call LLM SDK directly from routes or workflows (always via LiteLLM)
- Do NOT perform network I/O inside workflows (lives in activities)
- Do NOT reintroduce APScheduler/cron while Temporal schedules are available