# =============================================================================
# newsvoice — unified root Makefile (P7)
# =============================================================================
# Orchestrates both apps (apps/news, apps/gateway) and the shared
# libs/tts_contract from the repo root. App-local Makefiles still exist for
# per-app work; this file is the single entrypoint for ops that span the
# whole stack.
#
#   make help        list every target
#   make status      health of all 12 services + news API + TTS gateway
#   make check       lint + format-check + tests across both apps
#   make test        full pytest suite for news + gateway
#   make up / down   start / stop the unified deploy/docker-compose.yml stack
#
# Tooling defaults to each app's .venv. Override if your venv lives elsewhere:
#   make check NEWS_PY=/path/to/python GW_PY=/path/to/python
# =============================================================================

.PHONY: help venv install lint format format-check test test-news test-gateway \
        coverage check commit up down ps health logs logs-api logs-gateway \
        logs-worker logs-temporal status deploy clean urls

REPO_ROOT := $(shell git rev-parse --show-toplevel 2>/dev/null || pwd)

# --- Tooling (overridable) ---------------------------------------------------
NEWS_DIR   := apps/news
GW_DIR     := apps/gateway
CONTRACT   := libs/tts_contract
NEWS_PY    ?= $(NEWS_DIR)/.venv/bin/python
GW_PY      ?= $(GW_DIR)/.venv/bin/python
NEWS_RUF   ?= $(NEWS_DIR)/.venv/bin/ruff
GW_RUF     ?= $(GW_DIR)/.venv/bin/ruff

# --- Ports / compose ---------------------------------------------------------
API_PORT     ?= 8000
GATEWAY_PORT ?= 8001
COMPOSE_FILE ?= deploy/docker-compose.yml
DC            = docker compose -f $(COMPOSE_FILE)

help: ## Show all make targets
	@echo "newsvoice unified make targets"
	@echo ""
	@echo "Setup / quality"
	@echo "  make venv             Create news + gateway .venv with dev extras"
	@echo "  make install          Alias for venv"
	@echo "  make lint             ruff check across news, gateway, tts_contract"
	@echo "  make format           ruff format (in-place) across all packages"
	@echo "  make format-check     ruff format --check across all packages"
	@echo "  make test             pytest for news + gateway"
	@echo "  make test-news        pytest for apps/news only"
	@echo "  make test-gateway     pytest for apps/gateway only"
	@echo "  make coverage         pytest with coverage for both apps"
	@echo "  make check            lint + format-check + test (pre-commit gate)"
	@echo "  make commit MSG=..    Commit via scripts/auto_commit.sh"
	@echo ""
	@echo "Docker compose ($(COMPOSE_FILE))"
	@echo "  make up               Build and start the whole stack"
	@echo "  make down             Stop the whole stack"
	@echo "  make ps               Show service status"
	@echo "  make health           Curl news API + TTS gateway health"
	@echo "  make logs             Tail api + gateway + worker logs"
	@echo "  make logs-api         Tail news api logs"
	@echo "  make logs-gateway     Tail TTS gateway logs"
	@echo "  make logs-worker      Tail temporal worker logs"
	@echo "  make logs-temporal    Tail temporal logs"
	@echo "  make status           Unified health check (scripts/status.sh)"
	@echo "  make deploy [ENV=..]  Deploy via scripts/deploy.sh (default production)"
	@echo ""
	@echo "Utilities"
	@echo "  make urls             Print admin GUI, Temporal UI and gateway URLs"
	@echo "  make clean            Remove caches, coverage and build artifacts"

venv: ## Create news + gateway .venv with dev extras
	cd $(NEWS_DIR) && python3 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"
	cd $(GW_DIR) && python3 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"

install: venv ## Alias for venv

lint: ## ruff check across news, gateway, tts_contract
	$(NEWS_RUF) check $(NEWS_DIR)
	$(GW_RUF) check $(GW_DIR)
	$(GW_RUF) check $(CONTRACT)

format: ## ruff format (in-place) across all packages
	$(NEWS_RUF) format $(NEWS_DIR)
	$(GW_RUF) format $(GW_DIR)
	$(GW_RUF) format $(CONTRACT)

format-check: ## ruff format --check across all packages
	$(NEWS_RUF) format --check $(NEWS_DIR)
	$(GW_RUF) format --check $(GW_DIR)
	$(GW_RUF) format --check $(CONTRACT)

test-news: ## pytest for apps/news only
	cd $(NEWS_DIR) && PYTHONPATH=".:../../$(CONTRACT)" $(abspath $(NEWS_PY)) -m pytest -q

test-gateway: ## pytest for apps/gateway only
	cd $(GW_DIR) && PYTHONPATH="../../$(CONTRACT)" $(abspath $(GW_PY)) -m pytest -q

test: test-news test-gateway ## pytest for news + gateway

coverage: ## pytest with coverage for both apps
	cd $(NEWS_DIR) && PYTHONPATH=".:../../$(CONTRACT)" $(abspath $(NEWS_PY)) -m pytest -q \
		--cov=src --cov-report=term-missing:skip-covered
	cd $(GW_DIR) && PYTHONPATH="../../$(CONTRACT)" $(abspath $(GW_PY)) -m pytest -q \
		--cov=tts_api --cov-report=term-missing:skip-covered

check: lint format-check test ## Pre-commit gate: lint + format-check + test

commit: ## Commit via scripts/auto_commit.sh
	@test -n "$(MSG)" || (echo 'Usage: make commit MSG="type(scope): description"' && exit 1)
	./scripts/auto_commit.sh "$(MSG)"

up: ## Build and start the whole stack
	$(DC) up -d --build

down: ## Stop the whole stack
	$(DC) down

ps: ## Show service status
	$(DC) ps

health: ## Curl news API + TTS gateway health
	@echo "-- news-engine API --"
	@curl -sf http://localhost:$(API_PORT)/health && echo ""
	@echo "-- TTS gateway --"
	@curl -sf http://localhost:$(GATEWAY_PORT)/api/v1/health && echo ""

logs: ## Tail api + gateway + worker logs
	$(DC) logs -f api gateway worker

logs-api: ## Tail news api logs
	$(DC) logs -f api

logs-gateway: ## Tail TTS gateway logs
	$(DC) logs -f gateway

logs-worker: ## Tail temporal worker logs
	$(DC) logs -f worker

logs-temporal: ## Tail temporal logs
	$(DC) logs -f temporal

status: ## Unified health check (scripts/status.sh)
	./scripts/status.sh

deploy: ## Deploy via scripts/deploy.sh (ENV defaults to production)
	./scripts/deploy.sh $(ENV)

urls: ## Print admin GUI, Temporal UI and gateway URLs
	@echo "Admin GUI:   http://localhost:$(API_PORT)/ui"
	@echo "Temporal UI: http://localhost:8081"
	@echo "TTS gateway: http://localhost:$(GATEWAY_PORT)/api/v1"

clean: ## Remove caches, coverage and build artifacts
	rm -rf $(NEWS_DIR)/.pytest_cache $(NEWS_DIR)/.ruff_cache $(NEWS_DIR)/.coverage
	rm -rf $(GW_DIR)/.pytest_cache $(GW_DIR)/.ruff_cache $(GW_DIR)/.coverage
	find $(NEWS_DIR) $(GW_DIR) $(CONTRACT) -type d -name __pycache__ -prune -exec rm -rf {} +
	find $(NEWS_DIR) $(GW_DIR) $(CONTRACT) -type f -name "*.pyc" -delete
	rm -rf $(NEWS_DIR)/build $(NEWS_DIR)/dist $(NEWS_DIR)/src/news_engine.egg-info
