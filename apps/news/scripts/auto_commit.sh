#!/usr/bin/env bash
set -e

MSG="$1"

if [ -z "$MSG" ]; then
  echo "Error: Commit message required."
  echo "Usage: ./scripts/auto_commit.sh \"type(scope): description\""
  exit 1
fi

echo "==> Running ruff check..."
.venv/bin/ruff check . || ruff check .

echo "==> Running ruff format..."
.venv/bin/ruff format . || ruff format .

echo "==> Running pytest..."
.venv/bin/pytest -q || pytest -q

echo "==> Staging files..."
git add .

echo "==> Committing with message: $MSG"
git commit -m "$MSG"

echo "==> Done."
