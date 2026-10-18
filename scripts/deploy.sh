#!/usr/bin/env bash
set -euo pipefail

# Unified deploy for the whole newsvoice stack (P7). Builds and starts every
# service in deploy/docker-compose.yml (news-engine + TTS gateway), waits for
# both health endpoints, then registers the per-collection news schedules.
#
# Usage:
#   ./scripts/deploy.sh [environment] [--tts]
#     environment  label only, defaults to "production"
#     --tts        enable audio in the scheduled digest

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

ENV="${1:-production}"
SCHEDULE_TTS=""
if [[ "${2:-}" == "--tts" || "${SCHEDULE_TTS:-}" == "1" ]]; then
    SCHEDULE_TTS="--tts"
fi

COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.yml}"
DC=(docker compose -f "$COMPOSE_FILE")

_read_env() {
    local file="$1" key="$2" default="$3" val=""
    [[ -f "$file" ]] || { echo "$default"; return; }
    val="$(grep -E "^${key}=" "$file" | tail -n1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
    echo "${val:-$default}"
}

API_PORT="${API_PORT:-$(_read_env deploy/.env API_PORT 8000)}"
GATEWAY_PORT="${GATEWAY_PORT:-$(_read_env deploy/.env GATEWAY_PORT 8001)}"

wait_for() {
    local name="$1" url="$2"
    echo "==> Waiting for ${name} health (${url})..."
    for _ in $(seq 1 20); do
        if curl -sf "$url" >/dev/null 2>&1; then
            echo "==> ${name} healthy."
            return 0
        fi
        sleep 3
    done
    echo "${name} health check failed after 60s!" >&2
    return 1
}

echo "==> Deploying to ${ENV} environment..."
"${DC[@]}" up -d --build

if ! wait_for "news-engine API" "http://localhost:${API_PORT}/health"; then
    "${DC[@]}" logs api >&2
    exit 1
fi

if ! wait_for "TTS gateway" "http://localhost:${GATEWAY_PORT}/api/v1/health"; then
    "${DC[@]}" logs gateway >&2
    exit 1
fi

echo "==> Registering the per-collection schedules (each HH:MM comes from collections.toml)..."
# shellcheck disable=SC2086
"${DC[@]}" exec -T worker python -m src.workers.schedule ${SCHEDULE_TTS}

echo "==> Deployment successful."
