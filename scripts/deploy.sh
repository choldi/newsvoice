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
#
# Environment switches:
#   GPU=1   add the deploy/docker-compose.gpu.yml overlay (gateway reserves the
#           host GPU; requires nvidia-container-toolkit). Shape A on a GPU host.
#   MODAL=1 run with --profile modal so the Modal narration worker starts too
#           (parity with the pre-merge production stack).
#   QWEN3=1 start the self-contained qwen3 sidecar (profile qwen3) and point the
#           gateway at it. Requires GPU=1.
#   OMNIVOICE=1 start the OmniVoice sidecar (profile omnivoice) and point the
#           gateway at it. Requires GPU=1.
#   SIDECARS=1 start both sidecars and add the GPU overlay automatically
#           (equivalent to GPU=1 QWEN3=1 OMNIVOICE=1).
#
# Temporal namespaces are created before the workers start, so a fresh database
# or a restore does not leave every worker idle forever.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

_read_env() {
    local file="$1" key="$2" default="$3" val=""
    [[ -f "$file" ]] || { echo "$default"; return; }
    val="$(grep -E "^${key}=" "$file" | tail -n1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
    echo "${val:-$default}"
}

ENV="${1:-production}"
SCHEDULE_TTS=""
if [[ "${2:-}" == "--tts" || "${SCHEDULE_TTS:-}" == "1" ]]; then
    SCHEDULE_TTS="--tts"
fi

if [[ "${SIDECARS:-0}" == "1" ]]; then
    GPU=1
    QWEN3=1
    OMNIVOICE=1
    echo "==> SIDECARS=1: enabling qwen3 + omnivoice sidecars and the GPU overlay"
fi

COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.yml}"
export COMPOSE_FILE
DC=(docker compose -f "$COMPOSE_FILE")

if [[ "${GPU:-0}" == "1" ]]; then
    DC+=(-f deploy/docker-compose.gpu.yml)
    echo "==> GPU overlay enabled (deploy/docker-compose.gpu.yml)"
fi

if [[ "${MODAL:-0}" == "1" ]]; then
    DC+=(--profile modal)
    echo "==> Modal profile enabled (narration-modal-worker will start)"
fi

if [[ "${QWEN3:-0}" == "1" ]]; then
    DC+=(--profile qwen3)
    export QWEN3_TTS_REMOTE_URL="${QWEN3_TTS_REMOTE_URL:-http://gateway-qwen3:8000/api/v1}"
    echo "==> qwen3 sidecar enabled (gateway -> ${QWEN3_TTS_REMOTE_URL})"
fi

if [[ "${OMNIVOICE:-0}" == "1" ]]; then
    DC+=(-f deploy/docker-compose.omnivoice.yml --profile omnivoice)
    if [[ -z "${OMNIVOICE_REMOTE_URL:-}" ]]; then
        OMNIVOICE_REMOTE_URL="$(_read_env apps/gateway/.env OMNIVOICE_REMOTE_URL http://gateway-omnivoice:8000)"
        export OMNIVOICE_REMOTE_URL
    fi
    echo "==> OmniVoice sidecar enabled (gateway -> ${OMNIVOICE_REMOTE_URL})"
fi

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

echo "==> Starting Temporal and creating namespaces..."
"${DC[@]}" up -d --wait temporal
./scripts/create_namespaces.sh

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
