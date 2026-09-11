#!/usr/bin/env bash
set -e

ENV="${1:-production}"
if [ -n "${2:-}" ]; then
    # optional: pass --tts to enable audio in the scheduled digest
    SCHEDULE_TTS="$2"
else
    SCHEDULE_TTS="${SCHEDULE_TTS:-}"
fi

API_PORT="${API_PORT:-8000}"
echo "==> Deploying to $ENV environment..."

docker compose up -d --build

echo "==> Waiting for API health..."
HEALTH_OK=""
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf "http://localhost:${API_PORT}/health" >/dev/null 2>&1; then
        HEALTH_OK="true"
        break
    fi
    sleep 3
done

if [ -z "$HEALTH_OK" ]; then
    echo "Health check failed after 30s!" >&2
    docker compose logs api >&2
    exit 1
fi
echo "==> API healthy."

echo "==> Registering daily schedule (08:00 UTC)..."
docker compose exec -T worker python -m src.workers.schedule --hour 8 --minute 0 $SCHEDULE_TTS

echo "==> Deployment successful."