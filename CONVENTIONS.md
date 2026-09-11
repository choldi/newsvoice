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