#!/usr/bin/env bash
set -euo pipefail

# Unified pre-commit gate for the whole monorepo (P7).
# Runs ruff check + format + the full pytest suite for every package, then
# stages and commits. Mirrors apps/news/scripts/auto_commit.sh but covers
# news-engine, the TTS gateway and the shared tts_contract lib.
#
# Usage: ./scripts/auto_commit.sh "feat(scope): description"

MSG="${1:-}"
if [[ -z "$MSG" ]]; then
    echo "Error: Commit message required."
    echo 'Usage: ./scripts/auto_commit.sh "type(scope): description"'
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Tooling: prefer each app's .venv, fall back to a PATH binary. Every path can
# be overridden via env, e.g. NEWS_RUF=/path/to/ruff ./scripts/auto_commit.sh …
_bin() { # _bin <app-dir> <tool>
    local dir="$1" tool="$2"
    if [[ -x "${dir}/.venv/bin/${tool}" ]]; then
        echo "${dir}/.venv/bin/${tool}"
    else
        echo "${tool}"
    fi
}

RUF_NEWS="${NEWS_RUF:-$(_bin apps/news ruff)}"
RUF_GW="${GW_RUF:-$(_bin apps/gateway ruff)}"
PYTEST_NEWS="${NEWS_PYTEST:-$(_bin apps/news pytest)}"
PYTEST_GW="${GW_PYTEST:-$(_bin apps/gateway pytest)}"
CONTRACT_PY="${CONTRACT_PY:-python3}"

echo "==> ruff check (news, gateway, tts_contract)..."
"$RUF_NEWS" check apps/news
"$RUF_GW" check apps/gateway
"$RUF_GW" check libs/tts_contract

echo "==> ruff format (news, gateway, tts_contract)..."
"$RUF_NEWS" format apps/news
"$RUF_GW" format apps/gateway
"$RUF_GW" format libs/tts_contract

echo "==> pytest (news)..."
(cd apps/news && PYTHONPATH=".:../../libs/tts_contract" "$PYTEST_NEWS" -q)

echo "==> pytest (gateway)..."
(cd apps/gateway && PYTHONPATH=".:../../libs/tts_contract" "$PYTEST_GW" -q)

echo "==> Staging files..."
git add .

echo "==> Committing with message: $MSG"
git commit -m "$MSG"

echo "==> Done."
