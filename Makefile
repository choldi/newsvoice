.PHONY: help venv install lint format format-check test coverage check commit token
.PHONY: api worker schedule trigger urls
.PHONY: up down ps health logs logs-api logs-worker logs-temporal
.PHONY: restart-api deploy status clean

PY   ?= .venv/bin/python
RUF  ?= .venv/bin/ruff
API_PORT ?= 8000

help: ## Show all make targets
	@echo "news-engine make targets"
	@echo ""
	@echo "Setup / quality"
	@echo "  make venv            Create .venv and install project with dev extras"
	@echo "  make install         Alias for venv"
	@echo "  make lint            Run ruff check"
	@echo "  make format          Run ruff format (in-place)"
	@echo "  make format-check    Run ruff format --check"
	@echo "  make test            Run pytest suite"
	@echo "  make coverage        Run pytest with coverage report"
	@echo "  make check           lint + format-check + test (pre-commit gate)"
	@echo "  make commit MSG=..   Commit via scripts/auto_commit.sh"
	@echo "  make token           Generate an ADMIN_API_TOKEN (openssl rand -hex 24)"
	@echo ""
	@echo "Local dev (host .venv)"
	@echo "  make api             Run API with uvicorn --reload on :$(API_PORT)"
	@echo "  make worker          Run the Temporal worker from .venv"
	@echo ""
	@echo "Docker compose"
	@echo "  make up              Build and start all services"
	@echo "  make down            Stop all services"
	@echo "  make ps              Show service status"
	@echo "  make health          Curl the /health endpoint"
	@echo "  make logs            Tail api + worker logs"
	@echo "  make logs-api        Tail api logs"
	@echo "  make logs-worker     Tail worker logs"
	@echo "  make logs-temporal   Tail temporal logs"
	@echo "  make restart-api     Restart the api service"
	@echo "  make deploy [ENV=..] Deploy with scripts/deploy.sh (default production)"
	@echo "  make status          Run scripts/status.sh health checks"
	@echo ""
	@echo "Temporal tasks (docker exec)"
	@echo "  make schedule[ COLLECTION=name]  Register daily schedules"
	@echo "  make trigger[ COLLECTION=name] [TTS=1]  Run a digest pipeline manually"
	@echo "  make urls            Print admin GUI and Temporal UI URLs"
	@echo ""
	@echo "Maintenance"
	@echo "  make clean           Remove caches, coverage, and build artifacts"

venv: ## Create .venv and install project with dev extras
	python3 -m venv .venv
	$(PY) -m pip install -e ".[dev]"

install: venv ## Alias for venv

lint: ## Run ruff check
	$(RUF) check .

format: ## Run ruff format (in-place)
	$(RUF) format .

format-check: ## Run ruff format --check
	$(RUF) format --check .

test: ## Run pytest suite
	$(PY) -m pytest -q

coverage: ## Run pytest with coverage report
	$(PY) -m pytest -q --cov=src --cov-report=term-missing:skip-covered

check: lint format-check test ## Pre-commit gate: lint + format-check + test

commit: ## Commit via scripts/auto_commit.sh
	@test -n "$(MSG)" || (echo "Usage: make commit MSG=\"type(scope): description\"" && exit 1)
	./scripts/auto_commit.sh "$(MSG)"

token: ## Generate an ADMIN_API_TOKEN
	@openssl rand -hex 24

api: ## Run API with uvicorn --reload
	$(PY) -m uvicorn src.api.main:app --reload --port $(API_PORT)

worker: ## Run the Temporal worker from .venv
	$(PY) -m src.workers.run_worker

schedule: ## Register daily schedules (docker exec)
	@if [ -n "$(COLLECTION)" ]; then \
		docker compose exec -T worker python -m src.workers.schedule --collection $(COLLECTION); \
	else \
		docker compose exec -T worker python -m src.workers.schedule; \
	fi

trigger: ## Run a digest pipeline manually (docker exec)
	@if [ -n "$(COLLECTION)" ]; then \
		TRIGGER_ARGS="--collection $(COLLECTION)"; \
	else \
		TRIGGER_ARGS=""; \
	fi; \
	if [ -n "$(TTS)" ]; then \
		TRIGGER_ARGS="$$TRIGGER_ARGS --tts"; \
	fi; \
	docker compose exec -T worker python -m src.workers.trigger $$TRIGGER_ARGS

urls: ## Print admin GUI and Temporal UI URLs
	@echo "Admin GUI:  http://localhost:$(API_PORT)/ui"
	@echo "Temporal UI: http://localhost:8080"

up: ## Build and start all services
	docker compose up -d --build

down: ## Stop all services
	docker compose down

ps: ## Show service status
	docker compose ps

health: ## Curl the /health endpoint
	curl -sf http://localhost:$(API_PORT)/health && echo ""

logs: ## Tail api + worker logs
	docker compose logs -f api worker

logs-api: ## Tail api logs
	docker compose logs -f api

logs-worker: ## Tail worker logs
	docker compose logs -f worker

logs-temporal: ## Tail temporal logs
	docker compose logs -f temporal

restart-api: ## Restart the api service
	docker compose restart api

deploy: ## Deploy with scripts/deploy.sh (ENV defaults to production)
	./scripts/deploy.sh $(ENV)

status: ## Run scripts/status.sh health checks
	./scripts/status.sh

clean: ## Remove caches, coverage, and build artifacts
	rm -rf .pytest_cache .ruff_cache .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf build dist src/news_engine.egg-info