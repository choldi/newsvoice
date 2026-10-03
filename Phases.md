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
`temporal-ui` at `:8081` with no code or compose changes. Default debugging stays
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
- [x] **7.2 Cliente del gateway + render** — `NarrationTTSService` con health,
      catálogo de voces, `/tts` y `/conversations` (estrategia explícita
      `RenderStrategy.TURNS` por defecto, `CONVERSATIONS` solo con cast
      homogéneo); troceado cliente (el gateway no hace chunking); ensamblado y
      normalización a WAV canónico (mono 24 kHz); música local con ffmpeg
      (loop, fades, ducking); encode M4A; caché con sidecar; tests unitarios y
      tests de integración con ffmpeg real
- [x] **7.3 Workflow y cola propias** — `narration_activities.py` (resolve script +
      render con `audio_jobs.kind='narration'` y `digest_runs.narration_path`),
      `NarrationWorkflow` en `narration_workflows.py`, `run_narration_worker.py`
      en `NARRATION_TASK_QUEUE` con sus propias activities, disparo desde
      `DigestPipelineWorkflow` con `ActivityOptions(task_queue=...)` y actividad
      resuelta **por nombre**, `trigger_narration.py` para renders
      manuales, y `RunsRepository.set_narration_path`
- [x] **7.4 API y GUI** — `POST /api/admin/narration`,
      `GET /api/admin/narration/voices`, opt-in por colección en
      `collections.toml` + GUI, player de narración en el detalle de run
- [x] **7.5 Guion LLM + ops** — prompts, activity de guion, servicio
      `narration-worker` en compose, `status.sh`, Deploy.md y Architecture.md
- [x] **7.6 Entrega de narración + counts configurables** — counts configurables
      por colección (`main_story_count`, `headline_count`) propagados al digest,
      lógica y guion de narración; plantillas `gaming` para digests extendidos
      (5+10) y narración estilo diálogo; entrega de la narración M4A a los
      canales configurados; aviso de error cuando el clonado falla
      (`NARRATION_FALLBACK_TO_EDGE=false`); bump `NARRATION_SCRIPT_TIMEOUT`
      60s → 300s; GUI/admin actualizado con campos `narration_deliver_audio`,
      `main_story_count`, `headline_count`; ejemplo `examples/podcast-gaming/`
      (README, TOML, spec JSON, script ejemplo, verificador de feeds)
- [x] **7.7 Conversations por bloques + plantilla `ai`** — `conversations` troceado
      en requests de ≤4000 caracteres (`split_turns_into_blocks`, cortes en
      frontera de turno, nunca a mitad de frase) con 1 reintento por bloque y
      unión de WAVs canónicos con `concat_wavs` + `pause_ms`; settings
      `narration_conversation_*` en `.env.example`; `conversation_blocks` en
      `NarrationOut` y en el sidecar; plantilla `ai` en
      `DIGEST_TEMPLATE_ADDITIONS` y `NARRATION_SCRIPT_TEMPLATES` (prohíbe
      emotion tags, que `conversations` rechaza); ejemplo `examples/podcast-ai/`

**Success criteria:** una colección opt-in produce un M4A de N minutos con ≥2
voces distintas, música de fondo (opcional), cacheado por hash y **entregado por
el canal configurado**; si el gateway falla, el error se notifica al canal; los
counts son configurables por colección.

**Effort:** 8-10 hours.

- [ ] **7.8 Voz pública con Qwen3-TTS en Modal** — vía pública con derechos
      comerciales claros, en paralelo a la vía privada actual (OmniVoice + clones):
      `TTSBackend` = `OMNIVOICE` | `QWEN3_TTS` | `ZONOS2` (ZONOS2 behind gate);
      `VoiceSource` = `QWEN_DESIGN` | `RECORDING`; catálogo JSON versionable
      (`voice_profiles.json`, 24 perfiles desde 7.9) con guard anti-drift
      (`instruct_sha256`, `ref_text_sha256` en el manifiesto); flujo
      `VoiceCatalogWorkflow` → `design_voice_activity` (el diseño se ejecuta en
      la GPU box, ver 7.9) → `upload_voice_activity` → Modal Volume; local =
      fuente de verdad; dos task queues (`narration_local_task_queue`,
      `narration_modal_task_queue`) con enrutado por `tts_backend`; worker Modal
      creado pero NO levantado; dispatch en `synthesize_turn()` con
      `_post_modal_tts()` (import lazy); `atempo` para `speed` en QWEN3_TTS;
      descargo AI Act audible con interruptor por colección; presupuesto $1/día y
      $10/mes con cuatro capas (preflight + `cache_probe`, caps en runtime,
      `terminate` en `finally`, `modal_spend` + alerta `modal_budget`); `modal`
      como única dep nueva; `qwen-tts` vive en la Modal image (Python 3.12), no
      en `pyproject.toml`; gate ZONOS2 (`scripts/benchmark_zonos.py`) — solo se
      activa si pasa

**Success criteria:** la vía A (OmniVoice + clones) sigue intacta y sin cambios
audibles; la vía B produce un M4A con voz sintética propia, descargo AI Act y
coste registrado en `modal_spend`; el worker Modal está creado pero no levantado;
ningún audio publicado usa voces de terceros sin licencia.

**Effort:** 12-16 hours.

- [ ] **7.9 Diseño de voces en la GPU box (debwk)** — el diseño sale de Modal y
      se ejecuta en `debwk` (`100.75.148.68`, RTX 2000 Ada) con Qwen3-TTS
      VoiceDesign local: `run_voice_worker --role design` registra solo
      `design_voice_activity` (cola `VOICE_DESIGN_TASK_QUEUE`, sin DB ni
      `data/voices`; el audio vuelve gzipeado por la red), `--role catalog`
      corre en el servidor (`voice-catalog-worker`, docker-compose) con
      `VoiceCatalogWorkflow` + status/store/upload en
      `VOICE_CATALOG_TASK_QUEUE`; **ambas mitades en el namespace propio**
      `news-engine-voices` porque una actividad siempre se ejecuta en el
      namespace de su workflow; catálogo ampliado a **24 voces (16 en + 8 es)**
      con `ref_text` compartido por idioma (`languages.<lang>.ref_segments`,
      `VoiceProfile` exige que `ref_text` sea exactamente la lectura de los
      segmentos, 42/42 fonemas EN y 32/32 ES vía
      `scripts/check_phoneme_coverage.py`); `concat_wavs` y la síntesis viven
      en `src/core/audio.py` y `src/core/local_tts.py` (libres de numpy y de
      feedparser/aiosqlite, así el worker arranca con venv mínimo); la vía A y
      el render diario siguen en OmniVoice y `modal_tts` — Modal solo queda
      para el upload del Volume y el render.

**Success criteria:** `design_voice --all --upload` genera 24 WAV en
`data/voices/refs/` con `modal_cost_usd: 0.0`, ambos workers se conectan al
namespace `news-engine-voices`, `status` reporta 24 perfiles listos, y la vía A
no cambia ni audiblemente ni en tests.

**Effort:** 6-8 hours (setup de debwk, namespace, verificación y gate).

---

- [x] **7.9b Namespaces de podcast y worker de dispatch** — la generación de
      podcast pasa a tener namespace propio por backend
      (`news-engine-podcast-local`, `news-engine-podcast-modal`), porque una
      actividad Temporal siempre se ejecuta en el namespace de su workflow y el
      digest ya no puede invocar el render directamente. `DigestPipelineWorkflow`
      llama a una única `dispatch_narration_activity`
      (`src/workers/activities.py`) que inicia y espera `NarrationWorkflow` en el
      namespace destino, con id `narration-<collection>-<run_id>` para que un
      reintento se reenganche al render en vuelo en lugar de pagar un segundo;
      el audio sigue volviendo al digest por el resultado de la activity, así que
      la entrega no cambia y un fallo de narración nunca tumba el digest.
      `src/workers/podcast_target.py` mapea `tts_backend` → namespace + cola y lo
      comparten schedule, trigger de API y trigger de CLI; se corrige el hueco
      heredado `narration_local_task_queue` ≠ `narration_task_queue` (la cola
      local encolaba para siempre). `scripts/create_namespaces.sh` crea los tres
      namespaces de forma idempotente (repetir tras cualquier restore de
      Temporal).

**Success criteria:** `713 passed` con `ruff` limpio, el digest entrega audio vía
`dispatch_narration_activity` (los renders no se ejecutan en el worker de digest),
el id del workflow de narración queda fijado por test, y los tres workers se
conectan a namespaces distintos y verificables con
`docker compose exec -T temporal temporal operator namespace list --address temporal:7233`.

**Effort:** 3-4 hours.

---

## ANTI-PATTERNS TO AVOID

- Do NOT add collections/multi-user before the single pipeline is stable
- Do NOT use Postgres for application state in Phase 1-4 (Temporal's own
  `temporal-postgres` is fine; news-engine state stays in SQLite)
- Do NOT build a web UI before Phase 5
- Do NOT call LLM SDK directly from routes or workflows (always via LiteLLM)
- Do NOT perform network I/O inside workflows (lives in activities)
- Do NOT reintroduce APScheduler/cron while Temporal schedules are available