# PHASES.MD - Incremental Delivery Plan (Temporal-based)

> **Merge monorepo:** la unificación de `news-engine` con el gateway `tts-api` en un
> único repo desplegable en 1..N servidores se documenta en **[MERGE_PLAN.md](MERGE_PLAN.md)**
> como las fases **P0–P10**, en paralelo a las fases de este fichero.

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

**Extensión multi-usuario / RBAC (2026-10-08, plan aprobado por el usuario):**
- [x] **6.1 Modelos y migración** — `012_users_sessions.sql`: `users` (rol
      `user|viewer`, estado `pending|active|disabled`), `login_codes` (solo el
      hash sha256 del código, single-use) y `sessions` (token opaco, 12 h);
      `src/api/models/auth.py`; repos `UsersRepository`,
      `LoginCodesRepository`, `SessionsRepository`
- [x] **6.2 Sesión y middleware de roles** — `src/api/authz.py` como fuente
      única de verdad (`Role` admin>user>viewer, `required_role`/`has_role`,
      `PUBLIC_ROUTES`; una ruta sin regla en `/ui`+`GET`→viewer y el resto→
      admin); `src/api/auth.py` reescrito: cookie HttpOnly
      `news_engine_admin` con token de sesión en DB (12 h), identidad en
      `request.state.user/role`, 401 JSON en `/api/*`, 302 exacto a
      `/ui/login` en HTML, 403 con `required`/`role` en JSON o flash en la
      página; fallback retrocompatible con `ADMIN_API_TOKEN`/password
      (nadie se desconecta tras el deploy)
- [x] **6.3 Registro + OTP por email** — auto-registro público en
      `/ui/register` (email → `pending`) que el admin aprueba y asigna rol
      desde `/ui/users`; `src/api/services/login_codes.py`: código de 6
      dígitos, TTL 600 s, hash sha256 ligado al email, 1 solo uso, máx. 5
      intentos, cooldown de reenvío 60 s, purge si SMTP falla;
      `EmailService.send_login_code_email`; rutas `/ui/login/email` y
      `/ui/login/verify`; el admin local (ADMIN_USERNAME/ADMIN_PASSWORD_HASH)
      nunca usa OTP ni es fila de `users`
- [x] **6.4 Gating de plantillas por rol** — nav admin (Ajustes, Usuarios);
      `_render` inyecta `is_admin`/`can_operate` y los botones se ocultan:
      viewer solo lectura (sin POST salvo logout), user además herramientas,
      ejecutar/narrar, terminar runs y borrar trabajos de audio, admin
      además colecciones (CRUD+resync), voces y settings
- [x] **6.5 Página de usuarios** — `GET /ui/users` + acciones solo-admin
      (`approve` con rol, `role`, `disable`, `enable`, `delete`; deshabilitar
      o borrar purga sesiones)
- [x] **6.6 Tests** — `tests/unit/test_authz.py` (matriz
      required_role/has_role), `tests/unit/test_login_codes.py` (OTP:
      estados, hash, single-use, intentos, TTL, cooldown, purge) y
      `tests/integration/test_rbac.py` (registro→aprobación→OTP→sesión,
      permisos viewer/user/admin, 401/403)
- [x] **6.7 Docs** — `.env.example` (SMTP/LOGIN_CODE_*/ADMIN_*),
      Deploy.md (login, roles y nota de lockout), Architecture.md y
      CONVENTIONS.md

**Fase 6 (extensión RBAC) completada el 2026-10-08** en `phase-6-rbac`
(7 commits, pendiente de PR a `main`).

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

- [x] **7.8 Voz pública con Qwen3-TTS en Modal** — vía pública con derechos
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

> **Cerrada (2026-10-07).** Verificado en el árbol: `modal>=1.0.0` en
> `pyproject.toml`, `TTSBackend.OMNIVOICE|QWEN3_TTS|ZONOS2` + `VoiceSource`,
> 24 perfiles con guard `instruct_sha256`/`ref_text_sha256`, flujo
> `VoiceCatalogWorkflow → design → store → upload`, las dos task queues
> (`narration_local_task_queue`/`narration_modal_task_queue`), el worker Modal
> declarado tras el profile `modal` (no levantado), el dispatch
> `_post_modal_tts()` con `atempo`, presupuesto `modal_budget_usd_daily=1.0` /
> `monthly=10.0` con alertas, y el gate ZONOS2 con `scripts/benchmark_zonos.py`.

- [x] **7.9 Diseño de voces en la GPU box (debwk)** — el diseño sale de Modal y
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

> **Cerrada (2026-10-07).** Verificado en el árbol: `run_voice_worker` con
> roles `design`/`catalog` en colas y namespace propios, `voice-catalog-worker`
> en docker-compose, `data/voices/refs/` con 24 WAV + `manifest.json` con 24
> entradas, `concat_wavs` en `src/core/audio.py` y `design_voice` en
> `src/core/local_tts.py`, `ref_text` compartido por idioma y cobertura de
> fonemas con tests (`test_phoneme_inventory.py`, 42/42 EN + 32/32 ES). La
> ventana de diseño sigue siendo manual (`scripts/design_window.sh`).

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

- [x] **7.13 Verificación LLM del digest contra sus fuentes** — todo digest
      generado pasa por un gate duro **antes** de cualquier entrega: ni texto,
      ni TTS, ni narración salen si la verificación falla. Dos capas en
      `src/api/services/verification.py`: (1) *groundedness* determinista — toda
      cifra (moneda, %, fecha, número) del digest debe existir en un artículo,
      con matching por variantes (`1.200` = `1200`) y saneado de la estructura
      Markdown y de versiones de producto (`Llama 3.3`, `GPT-6`) para no dar
      falsos positivos; (2) *faithfulness* con juez LLM vía LiteLLM
      (`local | api | modal`, temperatura 0.3) que parte el digest en claims
      atómicos y los etiqueta `supported | contradicted | unsupported` con cita
      literal, ponderando por fiabilidad de la fuente. Opt-in por colección
      (`llm_verify = true`) con kill switch global (`LLM_VERIFY_ENABLED`), por lo
      que la fase se entrega inerte. `verify_digest_activity` persiste una fila
      por capa en la tabla `verifications` (migración `010`) y
      `DigestPipelineWorkflow` llama al gate justo después de generar el digest
      y antes del bloque de canales: si `passed` es falso, registra el run como
      `verification_failed` y lanza `ApplicationError` no reintentable.
      Fiabilidad por fuente con tablas inline en `feed_urls`
      (`{ url, reliability, hosts }`, retrocompatible con strings): una fuente
      bajo `MIN_SOURCE_RELIABILITY` se descarta **antes** del clustering, y el
      artículo se puntúa por el host de su propio `link` (no por el feed), con
      `hosts` para agregadores. `PipelineConfig.verify` se resuelve fuera del
      workflow en `pipeline_config_for` (schedule, trigger CLI y trigger API).
      GUI: casilla/umbral/backend + mapas de fiabilidad por colección, etiqueta
      `verification_failed` en el listado de runs y endpoint
      `GET /api/admin/runs/{id}/verifications`. Configuración y calibración en
      `Deploy.md` §11, variables nuevas en `.env.example`.

**Success criteria:** `745 passed` con `ruff` limpio; una cifra inventada en el
digest bloquea la entrega (`test_pipeline_with_verification_failure_blocks_delivery`),
un digest con todas las cifras en las fuentes entrega igual que antes
(`test_pipeline_with_verification_passes`), y las 21 pruebas de
`tests/unit/test_verification.py` cubren extracción de cifras, matching y cada
veredicto.

**Effort:** 4-5 hours.

---

- [x] **7.10 Resumen de URL (entrada por URL)** — `POST /api/summarize/url`
      arranca `UrlSummarizeWorkflow` y responde **202** con el `workflow_id`
      (configuración con workflow, decisión del operador): la descarga, la
      extracción, la generación y la entrega viven en actividades, así que el
      run sobrevive a un reinicio de la API y queda en `digest_runs` como
      cualquier otro (fallos incluidos). El endpoint está tras el middleware de
      admin: escribe en mis canales y su URL de origen la descarga el servidor,
      así que no puede quedar público. `src/api/services/url_article.py` baja la
      página con `httpx` (timeout, `url_fetch_max_bytes`, solo http/https) y
      extrae el cuerpo con `trafilatura` **en `asyncio.to_thread`** (CPU-bound),
      con fallback `favor_precision` → `favor_recall` y rechazo permanente de
      páginas sin cuerpo (muros, apps JS): resumir una shell solo produciría una
      alucinación a partir de texto de navegación. `UrlSummarizeWorkflow`
      (`fetch → guardar? → generate → verify → entregar → registrar`) reutiliza
      las activities del pipeline RSS y, si la colección tiene `llm_verify`,
      pasa el mismo gate de la Fase 7.13 antes de entregar — la verificación no
      se puede esquivar usando este endpoint. `store = false` por defecto;
      con `store = true` se guarda la URL **normalizada** en `articles`
      (`save_article_activity`), la misma clave que usa el dedup del RSS, así
      que una ingesta posterior de la misma historia no duplica. Destinos,
      prompt, counts y payload de verificación se resuelven fuera del workflow
      en `url_summarize_config_for` (`src/workers/schedule.py`), como
      `pipeline_config_for`. Nueva dependencia `trafilatura>=1.8` (aprobada por
      el operador). Configuración en `Deploy.md` §12 y `.env.example`.

**Success criteria:** `784 passed` con `ruff` limpio; `store` llega a `false`
por defecto y `true` solo cuando se pide
(`test_url_summary_defaults_are_store_false_and_deliver_true`), una URL no
http(s) da 422, una colección desconocida da 404 sin arrancar workflow, el
endpoint exige el token de admin (401 sin él), una página sin cuerpo falla el
workflow **y** registra el run como `failed` sin entregar nada, y una
verificación rechazada bloquea la entrega (`verification_failed`).

**Effort:** 3-4 hours.

- [x] **7.11 TTS de documentos largos (PDF/EPUB/TXT/MD)** — `POST
      /api/admin/documents/tts` (multipart, tras el middleware de admin) sube el
      fichero a `data/doc_tts/uploads/`, abre la fila en `audio_jobs`
      (`kind='doc_tts'`, sin migración: 008 ya indexa `UNIQUE(run_id, kind)`) y
      arranca `DocumentTtsWorkflow` en la cola propia
      `news-engine-doc-tts-queue`, servida por el contenedor `doc-tts-worker`
      (nuevo en compose): un render de 300 páginas no puede ocupar los slots del
      worker de digest. El workflow `extract → synthesize` es orquestación pura;
      `extract_document_activity` baja el texto a `data/doc_tts/text/<id>.txt`
      (los payloads de Temporal nunca cargan el libro entero) y
      `synthesize_document_activity` llama al servicio con heartbeat por trozo
      y `start_to_close_timeout` de 6 h. Extracción con `pypdf` (páginas en
      orden, rechazo explícito de PDF escaneados sin texto), `ebooklib` (orden
      del spine, sin la nav) y decodificado UTF-8/latin-1 para TXT; el Markdown
      pasa por el mismo `markdown_to_speech_text` que el digest — para eso
      `tts.py` expone ahora `resolve_edge_voice`/`synthesize_mp3`/
      `estimate_duration_seconds` como funciones públicas (mismo código, los
      tests de la Fase 7 siguen verdes). El servicio trocea en
      párrafos→frases→palabras (`doc_tts_chunk_chars`, sin perder una sola
      palabra), sintetiza `doc_tts_concurrency` trozos en paralelo con reintentos
      por trozo y une los MP3 en orden: cada trozo se cachea por hash y el
      fichero final por `(voz, idioma, texto)`, de modo que un reinicio retoma
      donde estaba. La entrega es la URL: `GET
      /api/admin/documents/tts/{id}` devuelve el estado y, si está
      `FINISHED_OK`, un enlace `GET /api/audio/doc/{id}?exp=&sig=` firmado con
      HMAC-SHA256 (`doc_tts_link_secret`, TTL de `doc_tts_link_ttl_seconds`),
      que es el único endpoint **no** protegido por el middleware de admin: el
      enlace se abre desde el móvil sin token, expira y no se puede forjar; sin
      secreto configurado el submit responde 503 en lugar de generar un trabajo
      inútil. Kill switches `DOC_TTS_ENABLED` y el secreto; límites
      `DOC_TTS_MAX_FILE_BYTES` (413) y `DOC_TTS_MAX_SOURCE_CHARS` (error
      explícito, nunca un libro truncado en silencio).

**Success criteria:** `873 passed` con `ruff` limpio; el registro de activities
del worker nuevo espeja al de digest (`test_run_doc_tts_worker.py`), una
subida sin token da 401, un `.docx` da 415, el archivo gigante da 413, un PDF
escaneado falla con `FINISHED_ERROR` y su motivo, el segundo `synthesize`
entero golpea la caché sin tocar el motor, un enlace válido sirve el MP3 sin
token de admin y uno manipulado/expirado da 403.

**Effort:** 5-6 hours.

- [x] **7.12 Worker profesor de idiomas** — `POST /api/admin/lessons`
      (multipart: notas `.md`/`.txt`, modo, idiomas, colección y el **cast en
      la petición** como `NarrationSpec` JSON o los campos `voice` (+ `voice2`
      en `dual`)) sube las notas a `data/lessons/uploads/`, abre la fila en
      `audio_jobs` (`kind='lesson'`, sin migración) y arranca `LessonWorkflow`
      en la cola propia `news-engine-teacher-queue`, servida por el contenedor
      nuevo `teacher-worker`: ni el LLM ni un render de 10+ minutos pueden
      ocupar los slots del worker de digest. El workflow `generate → render →
      deliver` vive en `src/workers/lesson_{activities,workflows}.py` (módulo
      propio, igual que narración). **generate**: valida las notas (extensión,
      `TEACHER_MAX_NOTES_CHARS`), construye el prompt con el modo y el cast
      (`src/core/prompts.py::build_lesson_system_prompt`), llama al LLM con
      LiteLLM a 0.7, parsea JSON tolerante a vallas y guarda
      `text/<id>.md` + `text/<id>.script.json` — solo viajan paths por
      Temporal; la única pieza de contenido en el workflow es el Markdown, que
      necesita para entregar (un workflow no lee ficheros). Validaciones
      permanentes (notas, modo `dual` sin 2 voces, roles desconocidos) dan
      `FINISHED_ERROR` sin reintentos; el fallo de red del LLM reintenta.
      **render**: `NarrationTTSService.render` con las MISMAS voces clonadas
      de la narración (resuelve `NarrationSpec.language` y los roles como en
      el resto del proyecto) y cierra el job; la activity construye la URL
      firmada (settings sí, workflows no). **deliver**: direccionalas activities
      de entrega **por nombre en la cola del digest** (`task_queue=` resuelto
      fuera del workflow), patrón ya documentado para narración: email con el
      `.md` en `attachments=[...]` (parámetro nuevo y genérico en
      `EmailService`) y Telegram con `sendDocument` (activity nueva
      `deliver_telegram_document_activity`, registrada en el worker de digest).
      Regla de audio: si `duration ≤ TEACHER_MAX_AUDIO_SECONDS` (300 s,
      configurable) el audio viaja adjunto/`sendAudio`; si no, el cuerpo y el
      caption llevan `/api/audio/lesson/<id>?exp=&sig=` — HMAC con el MISMO
      secreto que los documentos (`signed_links.build_signed_path` ahora
      acepta `prefix`), servido por la ruta pública nueva que confina el
      fichero a `narration_cache_dir`. Estados en
      `/api/admin/lessons/{id}` + `data/lessons/meta/<id>.json`. Modos:
      `mixed` (una voz, nativa + ejemplos en objetivo), `dual` (dos voces —
      `voice` + `voice2` en la GUI/API o el cast en JSON —, la primera en
      nativa y la segunda en objetivo; sin ambas, 422) y `native` (todo en
      nativa, la objetivo citada). La GUI pide las voces según el modo vía
      `GET /ui/fragments/lesson_voices`. Cola y contenedor
      (`teacher-worker`) añadidos a compose; sin dependencias nuevas.

**Success criteria:** `933 passed` con `ruff` limpio; el worker nuevo espeja
el de digest (`test_run_teacher_worker.py`: registra exactamente sus dos
activities y ninguna otra, y la cola es distinta de las otras tres); sin token
da 401, un `.pdf` da 415, `mode=dual` con una sola voz da 422, el audio de
640 s **no** se adjunta y su URL aparece en el cuerpo (`test_lesson_long_audio_becomes_a_link`),
las deliveries se ejecutan en la cola del digest (test con dos workers) y el
enlace de audio se sirve sin token pero un `sig` alterado da 403.

**Effort:** 6-8 hours.

- [x] **7.14 Verificación de audio por bloques con Whisper** — dos capas de
      verificación ASR sobre el audio **a punto de entregarse**, con
      `faster-whisper` (CPU, int8) + `jiwer`: el audio se decodifica con ffmpeg
      a 16 kHz, se trocea en bloques de `AUDIO_VERIFY_BLOCK_SECONDS`
      (`condition_on_previous_text=False` por bloque, heartbeat entre bloques)
      y el transcript se compara con el texto esperado **normalizado**
      (minúsculas, sin puntuación ni tags de emoción, así `3,5%` = `3.5%`).
      Texto esperado exacto: para el TTS del digest usa el MISMO helper de
      síntesis (`tts.speech_text_for_digest`, refactorizado para que
      `generate_tts` y la verificación no puedan divergir); para la narración,
      el script en orden + la divulgación de IA audible cuando la spec la pide
      (`_expected_narration_text`). Opt-in por colección (`audio_verify` +
      `audio_verify_threshold`, con round-trip en API/GUI y kill switch global
      `AUDIO_VERIFY_ENABLED`), resuelto **fuera** del workflow en
      `pipeline_config_for` (`PipelineConfig.audio_verify`) y en el dict de
      narración; solo se verifica si la colección entregaría audio. Gate
      **solo de audio** (decisión del operador): en `DigestPipelineWorkflow`
      una verificación fallida pone `audio = None` —el texto ya se entregó, es
      primario y nunca espera al audio— con lo que compresión, adjuntos,
      `sendAudio` y el `audio_path` de la run se saltan, dejando una entrada
      `channel: audio_verification / verification_failed` en `delivery_status`;
      para la narración, `render_narration_activity` se verifica a sí mismo en
      su propio worker y `NarrationWorkflow` devuelve `verification`, que el
      digest consulta antes de `_deliver_narration_audio` (una narración
      rechazada nunca tumba el digest, convención intacta). Ambas capas
      escriben en el ledger `verifications` (`method='asr_wer'`,
      `target='tts_audio' | 'narration_audio'`, `score=WER` —¡menor, mejor—,
      `passed` explícito y detalles con transcript, bloques y similitud);
      fallos imposibles de transcribir dan `verdict=error` y retienen el audio
      (fail-closed). Nuevas dependencias `faster-whisper>=1.1` y `jiwer>=3`
      (aprobadas); modelo en `data/whisper` (volumen, descarga única ~150 MB).
      Configuración y calibración en `Deploy.md` §15.

**Success criteria:** `965 passed` con `ruff` limpio; smoke real
(edge-tts → WER 0,083 con umbral 0,30, detectó `2027`→`2017`); los 16 tests de
`test_audio_verify.py` cubren normalización/troceado/umbral/audio silencioso;
`test_audio_verification_failure_withholds_only_the_audio` demuestra que el
texto se entrega igual y ningún canal recibe el audio; el hook de narración
(`test_narration_audio_verify.py`) verifica, persiste el ledger y devuelve el
veredicto sin opt-in no gasta CPU; y el bug de `audio=None` sobre la rama de
compresión (excepción determinista → reintento infinito del workflow task) está
cubierto por el propio gate.

**Effort:** 5-6 hours.

- [x] **7.15 qwen3_tts on-prem, respelling y verificación calibrada** — la
      narración puede renderizar `qwen3_tts` en el gateway local
      (`run_in_modal=false`) con voces clonadas de operador. El gateway ejecuta
      `qwen-tts` en un venv externo (torch + `transformers==4.57.3`, aislado de
      `pocket_tts`), montado por `deploy/docker-compose.gpu.yml` junto al
      intérprete/stdlib/include/libpython de Python 3.13 y la caché de
      HuggingFace; la imagen del gateway añade `gcc`/`g++` porque Triton compila
      kernels en la primera síntesis. Las voces entran con el importador
      idempotente `scripts/import_voice_dropin.py` (`voice.json` + WAV 5-30 s +
      transcripción exacta 100-600 caracteres): crea el perfil y la referencia
      en el gateway y registra `<name>__qwen3_tts`; los ids del catálogo se
      resuelven al clon en cada render. **Respelling**: antes de sintetizar,
      `resolve_narration_script_activity` pide al LLM un mapa
      `{término: pronunciación}`, guarda `tts_text` por segmento (sin tocar
      `text`) y actualiza el script almacenado; el render habla `tts_text` y la
      caché incluye el script completo. Solo aplica a `qwen3_tts`, con
      `RESPELLING_ENABLED`/`RESPELLING_TEMPERATURE=0.2`, y un fallo del LLM deja
      el render intacto. **Verificación ASR**: Whisper recibe `spec.language`,
      puntúa contra `text` y usa `delta = WER(text, tts_text)` para relajar el
      umbral (`base * (1 + delta * factor)`); `wer_secondary` queda como
      diagnóstico. Calibración `tech` 2026-10-26: 15/15 aceptadas, peor
      `base_necesario=0,1193`, umbral elegido `0,20`; dry-run real de 60
      artículos con `score=0,0793` y umbral efectivo `0,2159`, narración
      entregada y cero canales externos.

**Success criteria:** `1604 passed` con `ruff check` y `ruff format --check`
limpios; `test_respelling.py`, `test_narration_audio_verify.py` y
`test_narration_activities.py` cubren el mapa, la persistencia de `tts_text`,
el idioma de Whisper, el delta y el script sin `thinking`; la calibración y el
dry-run dejan filas `valid` en `verifications` con el umbral efectivo
registrado.

**Effort:** 8-10 hours.

- [x] **GUI de operación (extensión de 7.10-7.14)** — toda la funcionalidad
      reciente queda operable desde `/ui` sin tocar la API REST. **Herramientas**
      (`/ui/tools`,4 pestañas con polling htmx): resumen de URL, TTS de
      documentos, clases de idiomas y podcast, cada uno con su formulario y su
      tabla de trabajos con enlaces firmados (`/api/audio/{doc,lesson,narration}/...`,
      esta última ruta nueva). Los submits delegan en los **mismos servicios**
      que la REST (`submit_document`, `submit_lesson`, `start_narration_render`,
      extraídos de las rutas en este commit): una sola validación, dos caras
      (flash en la GUI, código HTTP en la API). **Cast de narración** en
      Colecciones: editor estructurado de filas rol (voz con `datalist` del
      catálogo) + campos del spec, con el JSON avanzado como fallback y
      conservación de `music`/`max_chars_per_request` al guardar, más el botón
      «Generar podcast ahora» (`/ui/collections/{name}/narrate`, guion a partir
      del último digesto). **Verificaciones** visibles en el detalle de cada
      ejecución (tabla `verifications` con score/umbral/apto y detalles
      plegables; nota «menor es mejor» para `asr_wer`) y chip ámbar
      `verification_failed`. **Editor de `.env`** (`/ui/settings`): esquema
      derivado de `Settings.model_fields` (~159 variables agrupadas por
      sección), validación **de todo-o-nada** construyendo `Settings` con lo
      enviado (tipo, rango, JSON, reglas extra como `LLM_VERIFY_BACKEND`),
      reescritura de solo las líneas cambiadas preservando comentarios y orden
      con copia `.env.bak` y escritura atómica, secretos nunca reflejados en el
      HTML (input vacío = conservar) y aviso de reinicio (los settings están
      cacheados por proceso). En el camino se corrigieron dos bugs
      preexistentes: `narration_spec|tojson` hacía 500 la página Colecciones con
      cualquier spec, y el serializador TOML descartaba la lista `roles` al
      guardar (**borraba el cast** de la colección).

**Success criteria:** `1023 passed` con `ruff` limpio; los4 submits de
Herramientas mapean errores a flashes (`test_ui_tools.py`, 16), el cast
hace round-trip completo sin perder campos del spec (`test_ui_cast_editor.py`,
9), el editor de `.env` valida de todo-o-nada dejando el fichero intacto si algo
falla y nunca filtra un secreto (`test_env_editor.py`22 +
`test_ui_settings.py` 6), y el panel de verificaciones pinta cada veredicto
(`test_ui_endpoints.py`, 30).

**Effort:** 6-8 hours.

- [x] **7.16 Adaptación fonética en las clases (por par de idiomas)** — el
      formulario de clase gana la casilla **Adaptación fonética**
      (por defecto apagada; oculta con `TEACHER_RESPELLING_ENABLED=false`):
      los términos que la clase cita del idioma que se enseña se reescriben en
      `tts_text` para que la voz de la lengua nativa los pronuncie como suenan
      en el original ("en francés, decimos **bonjur**"). **Las heurísticas
      pasan a ser datos**: tabla `phonetic_heuristics` (migración
      `015_phonetic_heuristics.sql`, semillas `en→es`, `en→fr` —los pares que
      la 7.15 tenía en código— y `fr→es` con `flag_all_foreign`), una fila por
      par `origen → objetivo` con sufijos, dígrafos, tecnicismos, palabras
      nativas, ejemplos `{término: respelling}` y banderas `ascii_only` /
      `flag_all_foreign` / `enabled`, editables en `/ui/phonetics` (solo
      admin, CRUD con validación ISO 639-1, par repetido y par `mismo →
      mismo`). `src/core/respelling.py` carga las filas (`load_heuristics`,
      con los constantes de la 7.15 como *fallback* para la narración; una
      fila apagada manda) y añade `respell_lesson_script`, **estricta**:
      par no configurado/apagado, cast ≠ `qwen3_tts` o fallo del LLM acaban
      en `FINISHED_ERROR` con el motivo. Aplicación **por rol**: en `dual`
      solo el profe de la lengua nativa (el de objetivo habla su idioma);
      en `mixed`/`native`, todos los segmentos. Los metadatos guardan el mapa
      completo **por segmento** y la unión de términos (meta `respelling`,
      expuesta en `GET /api/admin/lessons/{id}`): se almacena, no se
      previsualiza. Nuevos settings `TEACHER_RESPELLING_ENABLED` y
      `TEACHER_RESPELLING_TEMPERATURE` (independientes de los de narración),
      validación en el submit (403 apagado / 422 idiomas iguales, motor ≠
      qwen3_tts, cast `dual` sin rol con `language=<nativa>` —no se adaptaría
      nada—, par sin configurar) y se corrigió `.gitignore`: la regla
      `models/` sin anclar también ocultaba `src/api/models/*.py` de git
      (un fichero nuevo ahí no se habría commiteado).

**Success criteria:** `1671 passed` con `ruff check .` y `ruff format --check .`
limpios; suites nuevas `test_phonetic_heuristics.py` (19: seeds + drift guard
contra las constantes 7.15, parsing de textareas, CRUD y los tres estados de
`require_enabled_pair`), ampliaciones en `test_respelling.py` (+14: par en BD,
fila apagada, candidatos por par, prompt con los dos idiomas, adaptación
por rol/estricta y el cast `dual` sin rol nativo), `test_lesson_activities.py`
(+5: mapa en el script y en la meta, y los tres fallos a `FINISHED_ERROR`),
`test_lesson_service.py` (+8), `test_admin_lessons_endpoint.py` (+6 con el mapa
en el estado) y la GUI (`test_ui_phonetics.py`, 7 + `test_ui_tools.py`, 3 +
`test_authz.py`, reglas admin de `/ui/phonetics`).

**Effort:** 6-8 hours.

---

## PHASE 8 — TTS engine routing, engine leasing and synthesis-time estimation

**Goal:** adapt news-engine to the rebranded, multi-engine `tts-api` gateway so
each collection can pick its own engine, so a long document render and the daily
digests never fight over that engine, and so the operator can see how long a
synthesis will take.

**Decision record (2026-10-05, operador):**

| # | Pregunta | Decisión |
|---|----------|----------|
| 1 | Alcance | **Fase 8 nueva** (la 7 sigue con 7.8 / 7.9 abiertas) |
| 2 | Motores por colección | **edge + tts-api gateway + Modal** (los tres) |
| 3 | ¿Qué se encola? | **Solo el paso TTS** — un lease con prioridad |
| 4 | ¿Cómo cede el doc-TTS? | **Event-driven** — el digest levanta un flag de prioridad antes de pedir el lease |
| 5 | «Duración» | **Tiempo de síntesis en wall-clock** (el `duration_estimate` de longitud de audio se queda como está) |

**Scope:**
- [x] **8.1 Lease de motor con prioridad** — migración `011_tts_lease.sql`
      (`tts_leases` + `tts_lease_waiters` en el SQLite ya compartido por todos los
      contenedores), `src/api/services/tts_lease.py` (`TTSLeaseService`:
      `acquire/release/renew/add_waiter/has_priority_waiter/hold`), digest =
      prioridad 10 y render de documento = prioridad 0, FIFO en empate, TTL 300 s
      con tarea de renovación (una llamada `synthesize_mp3` de 5000 chars dura
      minutos), kill switch `TTS_LEASE_ENABLED`
- [x] **8.2 Enrutado de motor por colección** — `TTSBackend.EDGE`,
      `CollectionConfig.tts_backend` resuelto **fuera** del workflow en
      `pipeline_config_for` → `PipelineConfig.tts_backend` → payload de
      `generate_tts_activity`, servicio nuevo `tts_backends.py` (edge→MP3,
      gateway→WAV vía el cliente extraído de `NarrationTTSService._post_audio`,
      Modal→WAV con el guard de presupuesto de la 7.8), backend incluido en el
      hash de caché + `SIDECAR_VERSION=2`, nombre de fichero con extensión
      según backend, y timeout del endpoint admin backend-aware (hoy 120 s
      haría 504 en cualquier render del gateway)
- [x] **8.3 Round-trip de `tts_backend`** — `CollectionIn` (`extra="forbid"`),
      `_COLLECTION_KEYS`, `collections.html` (crear + editar) y
      `partials/_tts_options.html` con el catálogo de voces **del backend**
      elegido (`VoicesService` para el gateway, `load_profiles()` para Modal,
      `edge_tts.list_voices()` para edge); también `podcast_target` trata
      `edge` como destino local (defensivo)
- [x] **8.4 Doc-TTS cede y reanuda** — hooks `should_pause` / `on_heartbeat` /
      `on_paused` en `DocTTSService.synthesize_document`, comprobados antes de
      cada trozo: libera el lease → `audio_jobs.status='PAUSED'` → sondea cada
      5 s con heartbeats → re-adquiere → `GENERATING`; los trozos en vuelo no se
      interrumpen; join format-aware (`concat_wavs` para `.wav`, el `b"".join`
      actual solo vale para MP3)
- [x] **8.5 Estimación de tiempo de síntesis** — `src/api/services/tts_estimate.py`
      (`SynthesisEstimate.wall_seconds = wall_seconds_for(backend, chars_sin_caché)
      / concurrencia + trozos × overhead + join`; Modal reutiliza
      `budget.estimate_gpu_seconds` para que tiempo y coste no se contradigan),
      tasas por backend en settings, expuesto en
      `GET /api/admin/documents/tts/{id}` (`estimate_wall_seconds`, `eta_seconds`,
      `chunks_done`, `chunks_total`, `paused`) y en `/ui/tools`. El `eta_seconds`
      deja de creerle al modelo en cuanto hay medición: `elapsed / done × restantes`.
      **Decisión:** el submit (202) NO lleva estimación — el texto aún no se ha
      extraído, así que una cifra ahí sería una mentira; la fuente de verdad es
      la primera consulta de estado, que la escribe `extract_document_activity`.
      `TTSOut.estimate_wall_seconds` cubre el digest (0 cuando la caché acierta).
      Los trozos cacheados cuestan 0, que es lo que hace correcto el número en
      una reanudación.
- [x] **8.6 Docs y calibración** — `Architecture.md` (diagrama con lease y
      enrutado), `CONVENTIONS.md` (sección «TTS engines & engine lease»),
      `Deploy.md` §10 (motor por colección, operación del lease, calibración de
      `cps`), `collections.toml.example` y `.env.example`

**Not in scope (decisión explícita):**
- Narración y clases **no** toman el lease en esta fase (ya llevan su propio
  semáforo de gateway); es una línea por activity si se quiere un gate global.
- El **id fijo** del schedule (`digest-pipeline-<collection>`) no cambia: 8.1
  serializa el **paso TTS**, no el run. Un start de schedule que llega con el
  run anterior vivo sigue siendo descartado por Temporal → eso queda fuera de
  esta fase.
- El doc-TTS **sigue en Edge**; hacerlo apuntar al gateway es un cambio aparte
  (el gateway no trocea y `doc_tts_chunk_chars` habría que re-tunearlo).

**Success criteria:** `1134 passed` con `ruff` limpio. Verificado por tests:

- cambiar `tts_backend` en una colección regenera el audio con el motor nuevo y
  con la extensión que ese motor produce (`test_switching_backend_regenerates_and_changes_the_extension`);
  la caché anterior jamás se sirve porque el backend está en el hash
  (`SIDECAR_VERSION=2`).
- el lease serializa: el digest no sintetiza hasta que el render cede
  (`test_generate_tts_activity_queues_behind_a_document_render`) y el render se
  pone en `PAUSED`, espera y reanuda sin perder trozos
  (`test_render_yields_the_engine_and_resumes_after_the_digest`); un render
  íntegramente cacheado no consulta el gate.
- la estimación devuelve 0 s cuando todo está cacheado y un `eta_seconds`
  medido en cuanto termina el primer trozo (`test_eta_uses_the_measured_rate_once_chunks_finish`).
- el round-trip completo de `tts_backend` sobrevive TOML ↔ API ↔ GUI
  (`test_tts_backend_round_trips_through_the_api`,
  `test_collections_gui_round_trips_the_tts_backend`).

**Effort:** real, sobre 23-32 estimadas — los cuatro patrones, 7 commits y 109
tests nuevos (1027 → 1134).

**Desviaciones respecto al plan** (decisiones tomadas en la implementación):

- Los pasos 2 y 3 se commitearon juntos: `test_collection_keys_cover_every_model_field`
  (la guarda de CONVENTIONS) exige que `CollectionConfig`, `CollectionIn`,
  `_COLLECTION_KEYS` y el GUI viajen en el mismo commit.
- El submit (202) no devuelve `estimated_wall_seconds`: el texto aún no se ha
  extraído. Lo escribe `extract_document_activity` y lo sirve el GET de estado.
- No hay `doc_tts_max_pause_seconds`: la pausa la acota
  `start_to_close_timeout` (6 h) de la activity y la caché de trozos hace
  barato cualquier reintento; un ajuste extra que no aporta sería ruido.
- Narración y clases no toman el lease (ya llevan su propio semáforo de
  gateway); añadirlo es una línea por activity.

---

## PHASE 9 — Voces personalizadas y selección multi-engine

**Goal:** que el menú **Voces** permita crear una voz nueva personalizada,
probarla y listarla como "voz generada"; que **todas** las voces (clonadas +
generadas + stock de todos los engines) se ofrezcan en desplegable en las
opciones de generar audio; y que una selección que mezcle voces de 2 engines
distintos quede **bloqueada en frontend y backend**.

**Decision record (2026-10-07, operador):**

| # | Pregunta | Decisión |
|---|----------|----------|
| 1 | Qué genera el apartado | **Voz nueva personalizada** (perfil nuevo, hoy solo se leen perfiles) |
| 2 | Alcance del guard anti-mix | **Todas las selecciones de voces** (GUI + API) |
| 3 | Alcance de los listados | **Menú voces + todos los desplegables** |
| 4 | Desplegable dentro de "Generar voces" | **Picker para probar voces**; en las opciones de generar audio, voz siempre vía `<select>` |
| 5 | Ciclo de vida | La voz pasa a "generada" **al existir su WAV** en `data/voices/refs/` |
| 6 | doc-TTS | Dropdown **solo-edge** (hoy muestra voces gateway que se ignoran en silencio) |
| 7 | Desconocidos en el guard | Heurística permisiva (sin falsos positivos) + verificación estricta solo donde el catálogo ya está en memoria |

**Prerrequisitos operativos** (no son código; documentados en `Deploy.md`):
`VOICE_STAGING_API_KEY` compartida entre API y worker de diseño, ventana de
diseño abierta (`scripts/design_window.sh`), y `MODAL_ENABLED=true` solo para
sintetizar con voces Modal (sin él, "Probar" reproduce el WAV de referencia
local).

- [x] **9.1 Registro unificado** — `src/api/services/voice_registry.py`
      (`VoiceOption` con `id/label/language/engines/category`;
      `stock|cloned|generated`), endpoint nuevo `GET /api/admin/voices/all`
      (los existentes quedan intactos), `_normalize_voice_entries` y los
      `_tts_voice_options`/`_edge_voice_options`/`_modal_voice_options`/
      `_omni_options` propagan `engine`+`category` con degradación a `[]`.
      Verificado 2026-10-07: ruff limpio, suite completa 1162 passed.
- [x] **9.2 Guard multi-engine** — `src/api/services/voice_engine.py`
      (`engines_for_voice()` síncrono sin red: patrones Edge, alias/patrón/UUID
      gateway, nombres del catálogo Modal, unión de engines válidos;
      desconocido = permitir) + `assert_voice_engine`/`assert_same_engine`
      (`VoiceEngineError`); validadores en `CollectionIn`, `NarrationSpec`,
      `TTSIn`; guards en la GUI de colecciones y `_narration_spec_from_form`;
      `resolve_cast` **infiere `tts_backend`** de `voice`+`voice2` y rechaza
      mixtos (`SubmitError`); `submit_document` edge-only; frontend sin JS
      inline: opciones filtradas por motor (`cast_voices` datalist re-fetch,
      `_tts_options` ya por motor, doc-TTS solo edge) y fragmento de lecciones
      que deshabilita el botón si hay mezcla.
      Verificado 2026-10-07: ruff limpio, suite completa 1200 passed.
- [x] **9.3 Voz nueva personalizada** — `load_profiles()` fusiona
      `data/voices/custom_profiles.json` (atómico, gitignored, **sin** aplicar
      el bloque `languages` a los custom, duplicado = error) +
      `save_custom_profile()`; servicio `voice_catalog_admin`
      (`catalog_status()`/`start_generate()`/`generated_voices()`/
      `create_custom_voice()` compartidos REST-UI);
      `POST /api/admin/voices/custom` (422 perfil inválido, 409 nombre
      ocupado, profile guardado aunque falle el enqueue) y
      `POST /ui/voices/custom`; tarjeta **"Voces generadas"** (fragmento
      `voices_generated` con estado `pendiente|generada|desactualizada`
      + `<audio>` del WAV de referencia); `modal_options()` etiqueta lo
      custom como `generated` tras la fusión. El endpoint de audio
      `GET /ui/voices/audio/{name}.wav` (slug + `resolve()` anti-traversal)
      se implementó aquí por ser dependencia de la tarjeta (mover a 9.4 si
      se reordena).
      Verificado 2026-10-07: ruff limpio, suite completa 1237 passed.
- [x] **9.4 Probar voces** — ~~`GET /ui/voices/audio/{name}.wav`~~ (hecho en
      9.3); servicio `voice_preview` (`preview()` resuelve con
      `voice_for`+`synthesize` en orden de motores preferente por voz,
      frase fija por idioma, caché en `data/voices/samples/` y **fallback al
      WAV de referencia** para voces Modal cuando `MODAL_ENABLED` está
      apagado o la síntesis falla; voz desconocida = error, no otra voz);
      `POST /ui/voices/test` (fragmento HTMX con `<audio>` o error inline),
      `GET /ui/voices/sample/{filename}` (nombre validado +
      `resolve()` anti-traversal) y desplegable unificado **"Probar voces"**
      en `/ui/voices` alimentado por `list_all_voices()`.
      Verificado 2026-10-07: ruff limpio, suite completa 1261 passed.
- [x] **9.5 Listados en todas las opciones de generar audio** — lecciones y
      `voice-list` pasan a `list_all_voices()` (todas las voces, cada opción
      etiquetada `· motor · origen`, selección que ningún catálogo conoce
      conservada); el cast editor pasa de input+datalist a **`<select>` por
      fila** (decisión 4) con swap OOB de todas las filas vía
      `/ui/fragments/cast_voices?cast=&rows=` y valor actual fuera de
      catálogo conservado; colección y doc-TTS ya eran `<select>` del
      registro (doc solo-Edge). Docs: `Architecture.md` (sección
      «Voice registry, engine guard and custom voices (Phase 9)» + fila de
      Key Decisions) y `Deploy.md` (§8.6 ciclo de vida de voces custom y
      prerequisitos operativos, §16.6 menú Voces, §16.2 cast actualizado).
      Verificado 2026-10-07: ruff limpio, suite completa 1267 passed.
- [x] **9.6 Tabla unificada de voces** — decisión del operador (2026-10-07,
      "ampliar Fase 9"): unificar los listados de `/ui/voices` en **una tabla
      paginada y filtrable** (se retiran las tarjetas "Probar voces", "Voces
      clonadas" y "Voces de stock"; se conservan "Voces generadas" con poll
      15s, "Nueva voz personalizada" y "Clonar nueva voz"). Nuevo servicio
      `src/api/services/voice_search.py` (`matches`/`filter_voices`/
      `picker_order`/`language_options`/`paginate`): glob **unanchored** y
      case-insensitive —`Mich*clo` encuentra "Michael_sss en(glish) Voz
      clonada"`—, `*`→`.*`, `?`→`.` con literales escapados; filtro de idioma
      por prefijo ISO (`es`→`es`/`es-ES`); páginas de 25 (máx. 200) con clamp.
      Tabla en `partials/_voices_table.html` con resumen ("N voces · página
      X de Y"), columna Estado (badge del manifest), enlace al WAV de
      referencia, botón "Probar" por fila, estado vacío y pager con el estado
      de filtros url-encoded; `GET /ui/fragments/voices_table` re-renderiza
      solo el cuerpo (búsqueda con `keyup changed delay:300ms`, selects
      Tipo/Motor/Idioma/Tamaño por `change`); `ui_voices` reescrito con un
      solo `list_all_voices()`. Tests: `tests/unit/test_voice_search.py`
      (13) y `tests/integration/test_voices_table.py` (8); assert de la
      página de voces en `test_ui_endpoints` actualizado a la paginación.
      Verificado 2026-10-07: ruff limpio, suite completa 1288 passed.
- [x] **9.7 Pickers de voz con búsqueda en herramientas** — decisión del
      operador (2026-10-07, "ampliar Fase 9"): en la página Herramientas el
      doc-TTS y las lecciones sustituyen `<select>`/datalist por **pickers
      con búsqueda** (input glob + filtro de idioma + resultados en radios;
      **sin JS inline**, todo htmx paginado; catálogo completo en
      `voice_search` compartido). `partials/_voice_picker.html` +
      `_voice_picker_results.html` + `GET /ui/fragments/voice_picker`
      (helpers `_picker_context`/`_picker_results_context`, cap 20 con
      contador, orden generadas→clonadas→stock, selección actual siempre
      conservada y marcada, opción literal "Usar «q» tal cual" cuando hay
      query sin coincidencias); doc-TTS solo-Edge (decisión 6) y lecciones
      con guard: los radios viajan con el `hx-include` y el contenedor de
      resultados re-dispara `/ui/fragments/lesson_voices` al pulsar para
      recalcular el guard anti-mezcla (filtros tipados `voice_q` etc.
      sobreviven al re-render); se retira el datalist `voice-list`. Tests:
      `tests/integration/test_voice_picker.py` (10, incluye el ejemplo
      `Mich*clo`) y actualización de dropdowns/guard/ui-tools al radio
      `checked`.
      Verificado 2026-10-07: ruff limpio, suite completa 1299 passed.
- [x] **9.8 Herramientas: idioma de los pickers, preview del cast y borrado** —
      decisión del operador (2026-10-08, "ampliar Fase 9"): en la página
      Herramientas (a) el **doc-TTS** pasa de input de idioma a un **select
      único** (poblado con los idiomas del catálogo, antes del picker) que
      filtra las voces Edge — se elimina el select interno redundante del
      picker (`_picker_context(langs=False)`); (b) las **lecciones** usan
      selects `target_language`/`native_language` del catálogo que re-fetchean
      `#lesson-voices` y filtran cada rol por su idioma — 'dual' → nativo
      `native_language`/objetivo `target_language`, 'mixed'/'native' → solo
      el objetivo (`_picker_lang_param`, `_picker_context(lang_input=...)`
      para que la búsqueda debounce conserve el filtro); (c) la ventana de
      **cast avanzado** añade una **preview dinámica** del NarrationSpec JSON
      sincronizada con modo+idiomas+voces vía htmx (helpers
      `_lesson_preview_spec`/`_lesson_preview_ready`, `<pre id="spec-preview">`
      con hint "Selecciona una voz..." y swap OOB desde el fragmento, sin
      botón copiar ni JS); (d) cada trabajo de audio terminado gana un botón
      **Borrar** que elimina todo lo del run — audio + sidecar `.meta.json` +
      fila `audio_jobs` + upload + `text/` + `meta/` (y el script en lecciones)
      vía `src/api/services/audio_cleanup.py` + `POST /ui/tools/jobs/{kind}/{run_id}/delete`;
      la narración además resetea `digest_runs.narration_path`
      (`RunsRepository.set_narration_path(..., None)` con `delete_job`).
      Tests: `tests/integration/test_voice_picker.py` (+5), condiciones 9.8
      en `test_voice_dropdowns.py` y `test_ui_tools.py` (+5; borrado de
      doc/lesson/narración con ficheros en disco y fila DB).
      Verificado 2026-10-08: ruff format/check limpios, suite completa 1310
      passed.

**Fase 9 completada el 2026-10-07** (9.1-9.8, ver checklist arriba).

**Success criteria:** `pytest -q` en verde con `ruff check .`/`ruff format`
limpios; una voz creada desde la GUI queda en `custom_profiles.json`, su
workflow aparece en el namespace `news-engine-voices` y, con la ventana de
diseño abierta, produce `data/voices/refs/<nombre>.wav` audible desde la
tarjeta; mezclar voces de 2 engines en lecciones/cast/colección devuelve
flash/422 sin crear job ni guardar; el catálogo devuelto por
`GET /api/admin/voices/all` incluye clonadas + generadas + stock con `engine`.

**Effort:** 8-12 hours.

---

## FASE 10: Qwen3-TTS local (gateway) + switch local/nube

**Objetivo:** Permitir usar Qwen3-TTS tanto local (gateway `~/ov_api` con engine
`qwen3_tts`) como en nube (Modal) con un switch explícito por operación.

### Checklist
- [x] **10.1 Gateway qwen3_tts local** (depende de gateway) — engine `qwen3_tts`
      con subprocess worker al venv `~/qwen3-tts/venv`, `locations=["local"]`,
      cloning/design soportados, health check no-fatal si falta runtime,
      catálogo `/api/v1/engines` con capacidades y `locations`. (hecho en
      gateway `dd874e3`; tests añadidos). **Portado al monorepo**
      (`apps/gateway/tts_api`, rename incluido) en MERGE_PLAN **P3**:
      378 tests verdes, `ruff` limpio.
- [x] **10.2 Routing narración**: añadir flag `run_in_modal` a `NarrationSpec`
      (default `false`). Cuando `tts_backend == QWEN3_TTS` y `run_in_modal` es
      `false`, enrutar a gateway con `engine=qwen3_tts` (pin por voz) para
      síntesis; cuando `true`, usar Modal. Mantener comportamiento para otros
      backends.
- [x] **10.3 UI narración**: añadir checkbox "Ejecutar en Modal" en formularios
      de cast/colecciones (junto a Backend TTS) y en el formulario de clase de
      idiomas (`tools.html`). Persistir en `narration_spec`.
- [x] **10.4 Generación de voces (GUI)**: añadir checkbox "Ejecutar en Modal"
      en creación de voz custom (junto a "Diseñar ahora"), flag
      `run_in_modal` en `POST /admin/voices/catalog/generate` y flag `--modal`
      en el CLI `design_voice`. `false` (default) = generación local vía
      worker/GPU (flow actual, guarda la referencia en el gateway); `true` =
      `modal_client.design_voice` → función Modal `design_voice`
      (`src/core/modal_app.py`, Qwen3-TTS VoiceDesign en L4) con la semilla
      crc32 y el muestreo 0.7/0.9 del path local (Phase 10.9), y guard
      **non-retryable** si `MODAL_ENABLED=false` o faltan credenciales (sin
      caer silenciosamente al worker local). La **clonación** no aplica: vive
      en el gateway y no existe backend de clonado en Modal. Requiere
      desplegar el app Modal (`python -m modal deploy src/core/modal_app.py`)
      y `MODAL_ENABLED=true` en el host que lo ejecute. **Verificado en debwk
      (2026-10-31):** run real con `run_in_modal=true` → actividad
      `run_in_modal=True` → `Voice designed ... design_seconds=72.47
      sample_rate=24000` → perfil `generada`. Ojo: los métodos de clase de
      Modal se invocan vía `modal.Cls.from_name(...)` (el `Function.from_name`
      directo no resuelve los métodos de clase) y `modal_cost.measure` no
      rodea esta llamada (su sondeo de billing envenena al SDK; ver
      PENDING.md).
- [x] **10.5 Servicio catálogo/clone**: al registrar refs diseñadas en gateway,
      incluir `engine="qwen3_tts"`. Al clonar vía gateway, pasar `engine` cuando
      se desea forzar. Documentar en `Architecture.md`/`Deploy.md`. Hecho:
      `src/api/services/gateway_voices.py` resuelve nombres de perfil a un clon
      `qwen3_tts` (nombre `<voice>__qwen3_tts`) reutilizando el WAV de
      referencia y su `ref_text`, y cachea el UUID; `VoicesService.clone_voice`
      acepta `engines`.
- [x] **10.6 Tests**: unit/integration para routing local/nube, validación
      de spec, y que el render usa gateway cuando `run_in_modal=false`. Hecho:
      `tests/unit/test_gateway_voices.py` y el caso local qwen3_tts en
      `tests/unit/test_narration_tts.py`.
- [x] **10.7 Verificación**: `pytest -q`, `ruff check .`, `ruff format .`,
      reintento de lección que falla con Modal deshabilitado usando
      `run_in_modal=false` + voz en gateway qwen3_tts. Verificado en prod:
      `marie_fr` se resuelve a un clon `marie_fr__qwen3_tts` y sintetiza FR por
      el gateway local (200, 24 kHz) sin Modal.
- [x] **10.8 Catálogo FR + origen de voces**: añadir francés al catálogo
      committed (`voice_profiles.json`, 8 perfiles `fr_*` con su bloque
      `languages.fr.ref_segments`) y su inventario fonético (35 fonemas en
      `src/core/phoneme_inventory.py`). Corregir el merge de `list_all_voices`
      para que una voz del catálogo Modal conserve su origen `stock` cuando el
      gateway tiene un clon del mismo nombre (antes se etiquetaba `cloned` y
      desaparecía del filtro `stock` de qwen3_tts/zonos2). Catálogo: 32
      perfiles (16 en + 8 es + 8 fr).
- [x] **10.9 Calidad del diseño de voces**: `QwenVoiceDesign.generate` sintetiza
      ahora toda la referencia en **una sola llamada** (une `ref_segments` con un
      espacio) en vez de generar cada segmento por separado y concatenarlos con
      un silencio: samplear por segmentos daba un timbre distinto por llamada y
      dejaba una referencia "muddy" para clonar. Además se bajan los parámetros
      de sampling desde los defaults del modelo (temperature `0.9`→`0.7`,
      top_p `1.0`→`0.9`, configurables con `QWEN_TTS_DESIGN_TEMPERATURE` /
      `QWEN_TTS_DESIGN_TOP_P`) y se fija una **semilla estable por perfil**
      (`crc32("qwen-voice-design:<name>")`) para reproducibilidad.

**Success criteria:** narraciones QWEN3_TTS pueden renderizarse vía gateway
local sin Modal habilitado; UI expone el switch; lint + tests verdes.

**Effort:** 4-6 hours.


## FASE 11: Audiolibros M4B (Fase B2)

**Objetivo:** Convertir EPUB/PDF/TXT/MD en un M4B con capítulos, metadatos y
portada, con worker y cola propios, y descarga desde la GUI.

### Checklist
- [x] **11.1 Servicio**: extracción de capítulos, síntesis con el backend
      elegido, concatenación, loudnorm opcional, ffmetadata y portada.
- [x] **11.2 Workflow/worker**: `AudiobookWorkflow` + actividades y
      `run_audiobook_worker` en la cola `AUDIOBOOK_TASK_QUEUE`.
- [x] **11.3 API admin**: submit, estado y descarga en
      `/api/admin/audiobooks/...`; regla `Role.USER` para el submit.
- [x] **11.4 GUI**: formulario en `/ui/tools`, motor/voz/idioma, listado de
      jobs y descarga desde el fragmento de trabajos.
- [x] **11.5 Caché y atomicidad**: clave con autor/portada/loudnorm y
      publicación del M4B con `os.replace` desde un `.part`.
- [x] **11.6 Limpieza**: borrado de audio, portada, subida, metadatos y fila.
- [x] **11.7 Compose/docs**: worker en ambos compose, perfil `news` del split,
      `status.sh`, `.env.example`, `Deploy.md` y `CONVENTIONS.md`.
- [x] **11.8 Tests**: unitarios de atomicidad/caché y de integración de rutas,
      listado y borrado.
- [x] **11.10 Motores locales (sin Modal)**: los audiolibros nunca gastan
      Modal — `zonos2`/`voxcpm2` se rechazan en el submit (422), `qwen3_tts`
      se fija al engine **local** del gateway (`run_in_modal=False` en
      `voice_for`/`synthesize`, voz de catálogo → clon `<name>__qwen3_tts`) y
      Kokoro queda activo en el gateway vía `TTS_ENGINE=routed` +
      `TTS_EXTRA_ENGINES=kokoro,qwen3_tts,omnivoice_remote` (el factory solo
      honra extras en `routed`; `RoutedEngine.list_stock_voices` degrada por
      sub-engine para que un sidecar caído no oculte las voces). La GUI ofrece
      solo motores locales: edge / tts-api gateway / Kokoro / Qwen3 local.
- [x] **11.9 Verificación en prod**: EPUB real de 3 capítulos con portada
      incrustada renderizado en debwk con `engine=qwen3_tts` **local (GPU,
      sin Modal)**: `FINISHED_OK`, M4B de 60 s / 980 KB, capítulos
      `Capítulo 1..3` con tiempos y portada mjpeg 600×600, descarga 200 y
      borrado vía `/ui/tools/jobs/audiobook/{id}/delete` (fila, audio,
      portada, subida y metadatos fuera; status/download → 404). El primer
      intento destapó un bug real: la lista concat escritura rutas relativas
      y ffmpeg las resolvía contra el directorio de la lista (exit 254);
      corregido con paths absolutos.

**Success criteria:** el M4B se reproduce con capítulos y portada; un fallo de
ffmpeg no deja ficheros parciales; las rutas no duplican `/api`; lint + tests
verdes.

**Effort:** 10-14 hours.


## ANTI-PATTERNS TO AVOID

- Do NOT add collections/multi-user before the single pipeline is stable
- Do NOT use Postgres for application state in Phase 1-4 (Temporal's own
  `temporal-postgres` is fine; news-engine state stays in SQLite)
- Do NOT build a web UI before Phase 5
- Do NOT call LLM SDK directly from routes or workflows (always via LiteLLM)
- Do NOT perform network I/O inside workflows (lives in activities)
- Do NOT reintroduce APScheduler/cron while Temporal schedules are available