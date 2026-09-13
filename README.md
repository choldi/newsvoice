# news-engine

AI News Digest Engine — Daily curated news digest from RSS feeds using local LLM.

Generates 5 main stories + 5 headlines, delivered via Telegram.

## Architecture

- **Hub**: Temporal.io (orchestration, scheduling, retries)
- **Spokes**: FastAPI API + Temporal activities

## Quick Start

```bash
cp .env.example .env
docker compose up -d --build
```

## Services

| Service | Port | Description |
|---------|------|-------------|
| api | 8000 | FastAPI REST API + admin GUI at `/ui` |
| worker | — | Temporal workflow worker |
| temporal | 7233 | Temporal server |
| temporal-ui | 8080 | Temporal web UI |

## Admin GUI

Manage collections, schedules, run history, and voices from the browser at
`/ui` (login with `ADMIN_API_TOKEN` from `.env`). REST equivalents live under
`/api/admin/*` (header `X-Admin-Token`).

## Schedule a digest

```bash
docker compose exec worker python -m src.workers.schedule --hour 8 --minute 0
```

## Manual trigger

```bash
docker compose exec worker python -m src.workers.trigger --tts
```

## Testing

```bash
pytest -q
ruff check .
ruff format .
```

## Make targets

Common operations are wrapped in a `Makefile` (run `make help` for the full
list). Highlights:

| Target | Purpose |
|--------|---------|
| `make up` | Build and start all Docker services |
| `make api` / `make worker` | Run API (uvicorn `--reload`) / Temporal worker from `.venv` |
| `make schedule` | Register the daily Temporal schedules (docker exec) |
| `make trigger` | Run a digest pipeline manually (docker exec) |
| `make check` | Pre-commit gate: `lint` + `format-check` + `test` |
| `make commit MSG="feat(x): y"` | Commit via `scripts/auto_commit.sh` |
| `make status` / `make health` | Run health checks |
| `make deploy` | Deploy via `scripts/deploy.sh` (default `production`) |
| `make token` | Generate an `ADMIN_API_TOKEN` (`openssl rand -hex 24`) |

Optional arguments: `make schedule COLLECTION=tech`, `make trigger COLLECTION=tech TTS=1`,
`make deploy ENV=staging`, `API_PORT=8800 make api`.
