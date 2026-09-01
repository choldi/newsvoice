#!/usr/bin/env bash
set -e

ENV="${1:-production}"
echo "==> Deploying to $ENV environment..."

docker compose up -d --build

echo "==> Checking API health..."
sleep 2
curl -f http://localhost:8000/health || (echo "Health check failed!" && exit 1)

echo "==> Deployment successful."
