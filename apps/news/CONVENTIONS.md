# Project Conventions

## Language and Style
- Python 3.11+
- PEP 8
- Max line length 100

## Formatter & Linter
- Formatter: `ruff format`
- Linter: `ruff check`

## Type Hints & Docstrings
- Type hints: mandatory on all public functions
- Docstrings: Google style on all public functions

## Naming Conventions
- Files/modules: snake_case (e.g., `cluster.py`, `llm_client.py`)
- Classes: PascalCase (e.g., `ArticleCluster`, `DigestService`)
- Functions/variables: snake_case
- Constants: UPPER_SNAKE_CASE
- Pydantic models: PascalCase + suffix

## Input/Output/Database Models
- In: `ArticleIn` (request payload)
- Out: `ArticleOut` (response)
- DB: `ArticleDB` (persisted)

## API Design (FastAPI)
- One endpoint per file in `src/api/routes/`
- Each route file exposes a single `APIRouter`
- Endpoints are stateless and idempotent where possible
- Request/Response via Pydantic models, never raw dicts
- Errors: raise `HTTPException` with meaningful detail
- All responses include a top-level `status` field (`"ok"` / `"error"`)
- TTS endpoint returns JSON with `audio_path` (not binary)
- **Costly endpoints (TTS, LLM) live under `/api/admin/*`**, so
  `admin_middleware` requires the admin token. Keep a cheap endpoint behind a
  kill switch in `src/core/config.py` (`TTS_API_ENABLED`) and give removed
  paths a `410 Gone` alias for one release before deleting them
- Cached artifacts record the inputs that produced them in a JSON sidecar next
  to the file (`news_<col>_<yymmdd>.meta.json` → `sha256` of text + voice +
  language), and are written atomically (temp file + `os.replace`) so a partial
  file is never served as a valid cache hit

## Temporal (Workflows & Activities)
- Workflows live in `src/workers/workflows.py`, activities in `src/workers/activities.py`
- Workflows must be **deterministic**: no I/O, no randomness, no external calls
  — all side effects happen in activities via `workflow.execute_activity`
- Activities are plain `@activity.defn` functions calling `src/api/services/` directly
- Multi-argument activity calls use the `args=[...]` keyword, not extra positionals
- Use `workflow.unsafe.imports_passed_through()` for activity imports in workflows
- Do NOT use keyword args with the sandbox logger (`workflow.logger.info("msg", foo=1)`
  is unsupported — use an f-string)
- Every `execute_activity` gets a `start_to_close_timeout`
- Activity unit tests use `temporalio.testing.ActivityEnvironment`
- Workflow integration tests use `WorkflowEnvironment.start_time_skipping()`
  with stub activities registered under the SAME names as the real ones

## System Monitoring
- Checks live in `src/api/services/monitoring/checks.py` as async functions
  `(CheckContext) -> CheckResult`; register them in `MONITOR_CHECKS` to include
  them in every monitor run (no workflow/activity changes needed)
- Checks run inside one activity (`run_monitor_checks_activity`); the workflow
  (`SystemMonitorWorkflow`) never does I/O or check imports itself
- Alerts are **transition-based**: alert only when a check moves to
  `error`/`warning` (see `MonitorReporter.decide_alerts`); decide alerts **before**
  `record_last_statuses`
- `CheckResult.detail` is a `str` (never `None`) so results survive Pydantic round-trips
- Status vocabulary: `ok` | `warning` | `error` | `skipped` | `info`
- Tests never hit the network: inject fake `httpx.AsyncClient` responses per URL,
  stub `temporal_admin`, `runs_repo`, and `docker` on `CheckContext`
- Docker state is read over the UNIX socket (`DockerService`), read-only; no extra HTTP service

## Database (SQLite)
- Single file: `data/news.db`
- Use `aiosqlite` for async access
- Migrations: manual SQL files in `src/api/db/migrations/` (no Alembic for v1)
- Tables:
  - `articles(url PK, title, content, fetched_at)`
  - `clusters(id PK, fingerprint JSON, first_seen, last_seen)`
  - `deliveries(cluster_id, delivered_at, material_update BOOL)`

## LLM Calls
- Always via LiteLLM wrapper in `src/core/llm.py`
- Never call OpenAI/llama.cpp SDK directly from routes
- All prompts stored as constants in `src/core/prompts.py`
- Temperature: 0.3 for summaries, 0.7 for clustering judgment

## Configuration
- `pydantic-settings` reading from `.env`
- Required vars: `LLM_API_BASE`, `LLM_MODEL`, `TELEGRAM_CHAT_ID`,
  `TELEGRAM_BOT_TOKEN`, `TEMPORAL_HOST`, `TEMPORAL_NAMESPACE`,
  `TEMPORAL_TASK_QUEUE`, `RSS_FEED_URLS`
- No hardcoded secrets; no default prod values
- `tts_*` is the digest audio namespace: it talks to edge-tts directly by
  default and, since Phase 8, to `tts_api_base_url` whenever a collection picks
  `tts_backend = "omni_voice"` — which is also what `VoicesService` uses for the
  GUI voice catalog. The Phase 7 narration pipeline keeps its own `narration_*`
  namespace (separate gateway URL, cache dir, task queue) and never repoints
  `tts_api_base_url`, so moving the digest's gateway never moves the cast's.

## Narration (Phase 7)
- Domain models live in `src/api/models/narration.py`; they carry no I/O and
  mirror the TTS gateway bounds (`speed` [0.5, 2.0], `pause_ms` [0, 5000],
  two-letter ISO 639-1 languages, `EMOTION_TAGS` from `src/core/prompts.py`)
- Audio jobs are discriminated by `audio_jobs.kind`: `'tts'` for the digest
  single-voice job, `'narration'` for the multi-speaker one. Every
  `AudioJobsRepository` call takes an explicit `kind` (default `'tts'`)
- Narration audio goes to `narration_cache_dir` (never `tts_cache_dir`) with a
  `narrate_<collection>_<yymmdd>_<hash8>` stem, so WAV/M4A files and their
  `.meta.json` sidecars cannot collide with the digest TTS cache. The hash covers
  the spec, the script, the render strategy and the music bed, so two different
  renders of the same piece never overwrite each other
- `digest_runs.narration_path` is written by the narration worker with
  `RunsRepository.set_narration_path` (a targeted UPDATE, never a full
  `record_run`, which would reset the digest counters) and preserved by
  `record_run` via `COALESCE`; `audio_path` stays reserved for the digest TTS.
  Because of this, `DigestPipelineWorkflow` records the run **before**
  dispatching the narration: annotating a row that does not exist yet loses the
  path silently
- Narration runs on its own task queue served by `narration-worker`
  (`src/workers/run_narration_worker.py`, entry point
  `python -m src.workers.run_narration_worker`). Cross-queue calls from a
  workflow use `ActivityOptions(task_queue=...)` and address the activity **by
  name**, because a worker only registers its own module
- `PipelineConfig.narration` is the opt-in: a dict with at least `task_queue`
  and `spec` (plus `script`/`script_id`/`strategy`). The kill switch
  (`narration_enabled`) and the per-collection choice are resolved **outside**
  the workflow, because a workflow cannot read settings. `None` means the
  collection does not narrate
- A narration failure is recorded in `delivery_status` with
  `channel: "narration"` and never fails the digest: the text digest and the
  TTS audio are already delivered by then
- Narration activities live in `src/workers/narration_activities.py`, not in
  `activities.py`: the digest worker must keep registering exactly the digest
  activity set. `tests/unit/test_run_worker.py` and
  `tests/unit/test_run_narration_worker.py` mirror each other to keep both
  registrations in sync
- The render strategy is explicit, never inferred: `RenderStrategy.TURNS`
  (default) synthesizes every turn, honors per-turn `speed`/`emotion` and the
  configured pauses; `RenderStrategy.CONVERSATIONS` uses a single
  `/conversations` request and therefore rejects scripts with per-role speeds or
  emotions instead of silently ignoring them
- Narration output is always canonical WAV (mono, 24 kHz, PCM s16le) before it
  is mixed, compressed or cached. ffmpeg must write to a **temporary file**, not
  a pipe: on a non-seekable output ffmpeg cannot patch the RIFF sizes and leaves
  `0xFFFFFFFF` placeholders, which makes the audio look hours long. WAV helpers
  therefore read the real payload length (`_data_span`) instead of the declared
  frame count
- The music bed is cosmetic: if ffmpeg fails, `NarrationTTSService._mix_music`
  logs a warning and returns the narration unmuted rather than losing the render.
  A missing or escaping track is an error, not a silent fallback.

- **Edge TTS fallback for narration:** two separate settings control the fallback:
  - `NARRATION_FALLBACK_TO_EDGE` (default `true`): governs single-voice narration
    renders (`turns` strategy with one voice). When `true`, a gateway failure
    triggers a transparent Edge TTS retry; when `false`, the failure propagates.
  - `NARRATIONS_DIALOG_FALLBACK_TO_EDGE` (default `false`): governs dialog/podcast
    renders (two or more distinct voices, or the `conversations` strategy). When
    `false` (default), a gateway failure **does not** fall back to Edge; instead it
    propagates and triggers the error-alert delivery. This prevents two cloned
    voices from collapsing to the same stock Edge voice. The dialog setting
    overrides the general one for dialog renders.

## TTS engines and the engine lease (Phase 8)

- One vocabulary for engines across the project: `TTSBackend`
  (`src/api/models/narration.py`) = `edge` | `omni_voice` (the rebranded
  `tts-api` gateway) | `qwen3_tts` | `zonos2` | `voxcpm2` (gated). `TTSBackend.EDGE` was added in
  Phase 8; `podcast_target` must map it to the **local** namespace/queue, never
  to Modal.
- The digest engine is a per-collection choice (`tts_backend` in
  `collections.toml`) resolved **outside** the workflow in `pipeline_config_for`
  → `PipelineConfig.tts_backend` → the `generate_tts_activity` payload. A
  workflow cannot read settings, and the schedule/trigger are what know the
  collection — same rule as `verify`, `audio_verify` and `narration`.
- **Round-trip rule** (same as any new collection field): `CollectionIn`
  (`extra="forbid"`), `_COLLECTION_KEYS`, `collections.html` (create + edit) and
  `partials/_tts_options.html` + `routes_ui.py::ui_fragment_tts_options` in one
  commit. `tests/unit/test_collections_repo.py::test_collection_keys_cover_every_model_field`
  is the guard; it fails the build if the model and the serializer drift.
- `voice` means different things per engine. `tts_backends.voice_for` resolves
  it: edge via `resolve_edge_voice`, gateway via the Edge→stock alias table,
  Modal against `voice_profiles.json`. A **foreign** id (an Edge ShortName left
  over after switching engines) resolves to the language default and logs;
  a real typo raises `TTSBackendError`, which activities turn into a
  non-retryable `ApplicationError` and HTTP turns into 422.
- The engine is part of the cache key (`_content_hash(..., backend)`,
  `SIDECAR_VERSION = 2`), so flipping a collection never serves another engine's
  bytes. **Fallback audio is never cached**: `TTS_FALLBACK_TO_EDGE` writes the
  file but skips the sidecar, so the next run retries the requested engine
  instead of pinning the degraded answer.
- Output follows the engine: `.mp3` for edge, `.wav` for the gateway and Modal.
  Everything downstream (`AudioCompressService`, `verify_audio`, the Telegram
  sender) is ffmpeg/extension-driven; only the cache filename and the doc-TTS
  chunk join ever had to care.

### The engine lease

- `src/api/services/tts_lease.py` + migration `011_tts_lease.sql`. The lease
  lives in the shared SQLite file because the digest worker and the doc-tts
  worker are **different containers**: an `asyncio.Semaphore` inside either one
  coordinates nothing.
- Priorities: `PRIORITY_RENDER = 0` (documents, lessons, narration),
  `PRIORITY_DIGEST = 10` and `PRIORITY_DESIGN = 20` — the voice design window
  (merge P8) queues ahead of both. A **live lease is never stolen** — hand-over
  goes through the yield protocol, and the TTL is only the safety net for a
  holder that stopped renewing.
- The digest writes its row into `tts_lease_waiters` **before** asking for the
  engine. That row *is* the event: a render polling `waiter_above(0)` lets go
  the moment it appears, so the two never synthesize concurrently. The waiter
  has its own TTL so a digest that dies cannot pause books forever.
- Holders renew every third of `TTS_LEASE_TTL_SECONDS` inside `hold()`; a
  single `synthesize_mp3` of a 5000-char digest is one long call and would look
  abandoned otherwise. Never import this module from a workflow.
- Long renders use `RenderGate`: `should_pause()` before each chunk (inside the
  semaphore, right before the call — a check made earlier runs once at t=0 and
  never sees a digest arriving later), then `yield_and_wait()` releases, reports
  `PAUSED`, waits, re-acquires and reports `GENERATING`. Chunks already in
  flight are never interrupted; at most `doc_tts_concurrency` of them overlap.
- `note` (migration `014_tts_lease_note.sql`) records *why* the engine is held:
  the design window claims with `note="design-window"`, the digest and renders
  keep `""` and are identified by owner. Read it via `holder_info()` (plus
  `waiters()` for the queue); renewals preserve it, a takeover overwrites it.
- The design window is claimed from the host by
  `python -m src.workers.design_lease {open,status,close}` — cwd = app root, so
  it resolves `data/news.db`. The voice workers still never touch the
  database; `design_window.sh` runs the CLI before stopping the gateway and
  releases it in the trap after restoring it (`DESIGN_WINDOW_NO_LEASE=1` skips
  this on Shape B hosts without the shared DB).
- `TTS_LEASE_ENABLED=false` restores the previous, unserialized behaviour on
  every path (all methods become no-ops).

### Wall-clock estimate (Phase 8.5)

- `src/api/services/tts_estimate.py` answers "how long will I wait", which is a
  different question from `duration_estimate` ("how long is the audio").
  Cached chunks cost **0** — that is what makes the number right on a resumed run.
- Rates live in settings (`TTS_EDGE_CHARS_PER_SECOND`,
  `TTS_GATEWAY_CHARS_PER_SECOND`); Modal reuses `budget.estimate_gpu_seconds` so
  the time and the cost models can never disagree. Calibrate them from
  `audio_jobs.elapsed_seconds`.
- `eta_seconds(estimate, progress)` prefers the **measured** rate as soon as one
  chunk has finished; the model is only the answer before the first one.

## Collections (Phase 5)
- Collections live in `collections.toml` (see `collections.toml.example`),
  loaded by `src/core/collections.py` via stdlib `tomllib` — no new dependency
- Each `[[collections]]` entry = one independent digest on its own Temporal
  schedule, with fields: `name`, `time` (HH:MM UTC), `feed_urls`,
  `tts_enabled`, `deliver_audio`, `podcast_behavior`, `language`, `voice`,
  `prompt_template`, `chat_id`, `enabled`, plus the delivery fields
  `email_recipients`, `whatsapp_recipients`, `whatsapp_group_id`,
  `delivery_channels` and the optional `logic_delivery` block
- **Round-trip rule:** a field of `CollectionConfig` must survive the admin GUI
  *and* the REST API. When adding one, update all three in the same commit:
  `CollectionIn` (with `extra="forbid"`, so a silent drop becomes a 422),
  `_COLLECTION_KEYS` in `src/api/services/collections_repo.py` (guarded by
  `tests/unit/test_collections_repo.py`), and `collections.html` +
  `routes_ui.py` form fields
- Shared defaults go under `[collection_defaults]`; entries override them
- Collection names must be unique and non-empty; `enabled = false` pauses a
  collection without deleting the config
- With no `collections.toml`, an implicit `default` collection is built from
  `RSS_FEED_URLS` (legacy single-digest behavior)
- One Temporal schedule per collection: id `daily-digest-<name>`, workflow id
  `digest-pipeline-<name>`, run via `python -m src.workers.schedule`
- Manual one-off run per collection: `python -m src.workers.trigger --collection <name>`
- Dedup, clusters, and deliveries are **collection-scoped** in the shared SQLite
  DB (migration `003`); `PipelineConfig` carries `collection` through the workflow
- Per-collection prompt templates are registered in `src/core/prompts.py` under
  `DIGEST_TEMPLATE_ADDITIONS` (built-in: `default`, `finance`, `health`,
  `investigative`, `gaming`)

## Auth and Roles (Phase 6)
- Authorization decisions live ONLY in `src/api.authz`: `required_role(method, path)`
  and `has_role(actual, required)` with `Role` (admin > user > viewer). Routes
  never hand-roll role checks; a new endpoint is safe by default (unlisted
  `/ui`+`/api` writes require admin) and only joins `_ROLE_RULES` when it
  needs a different level. Routes that must be reachable with no session go
  into `PUBLIC_ROUTES` deliberately — never by accident.
- The middleware in `src.api.auth` runs on every request: it injects
  `request.state.user` / `request.state.role` and answers 401/403. For
  `/api/*` the answer is JSON (`{"status": "error", ...}` with
  `required`/`role` in the 403 detail); for HTML it is a redirect (exact
  302 to `/ui/login` when anonymous, flash redirect when under-privileged).
- Sessions: mint them only via `create_session` / `destroy_session`
  (opaque random token, HttpOnly cookie `news_engine_admin`, 12 h row in
  `sessions`). Never put passwords or long-lived secrets in the cookie, and
  never store a password for a non-admin user — they log in with the
  emailed one-time code from `src.api.services.login_codes`.
- Templates gate affordances with the flags `_render` injects: `is_admin`,
  `can_operate` (admin or user), `role` and `current_user`. Hiding a button
  is UX only — the middleware is what actually denies the request, so both
  must agree (see `tests/integration/test_rbac.py`).

## Logging
- `structlog` with JSON output (workflows use the Temporal sandbox logger)
- Levels:
  - ERROR (failures)
  - INFO (endpoint calls, LLM calls)
  - DEBUG (payloads)
- Never log full article content at INFO

## Testing
- pytest + pytest-asyncio (`asyncio_mode = "auto"`; write `async def` tests and `await`)
- Unit tests for every service (mock LLM, mock DB)
- Integration tests per endpoint using TestClient
- Workflow integration tests with `WorkflowEnvironment`
- Coverage target: 80% on `src/api/services/`

## GIT and Commits
- Conventional Commits: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`
- One logical change per commit
- Use `./scripts/auto_commit.sh "<type>(<scope>): <message>"`
- Branch naming: `phase-1-mvp`, `phase-2-dedup`, `phase-3-cluster`, `phase-4-docs`