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
- In: `ArticleIn` (from n8n)
- Out: `ArticleDB` (persisted)
- DB: `DigestOut` (to n8n)

## API Design (FastAPI)
- One endpoint per file in `src/api/routes/`
- Each route file exposes a single `APIRouter`
- Endpoints are stateless and idempotent where possible
- Request/Response via Pydantic models, never raw dicts
- Errors: raise `HTTPException` with meaningful detail
- All endpoints return JSON; TTS endpoint returns JSON with `audio_path` (not binary)

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
- Required vars: `LLM_API_BASE`, `LLM_MODEL`, `TELEGRAM_CHAT_ID`
- No hardcoded secrets; no default prod values

## Logging
- `structlog` with JSON output
- Levels:
  - ERROR (failures)
  - INFO (endpoint calls, LLM calls)
  - DEBUG (payloads)
- Never log full article content at INFO

## Testing
- pytest + pytest-asyncio
- Unit tests for every service (mock LLM, mock DB)
- Integration tests per endpoint using TestClient
- Coverage target: 80% on `src/api/services/`

## GIT and Commits
- Conventional Commits: `feat:`, `fix:`, `refactor:`, `docs:`
- One logical change per commit
- Branch naming: `phase-1-mvp`, `phase-2-dedup`, `phase-3-cluster`

## N8N Integration
- n8n calls endpoints via HTTP POST with Content-Type: `application/json`
- All responses must include a top-level status field: `"ok"` | `"error"`
- Error responses include `error_code` and `message`
- n8n workflow JSONs versioned in `n8n/workflows/` with suffix `_v1.json`, `_v2.json`