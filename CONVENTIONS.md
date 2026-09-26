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
  `DIGEST_TEMPLATES` (built-in: `default`, `finance`, `health`, `investigative`)

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