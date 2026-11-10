#!/usr/bin/env bash
set -euo pipefail

# Unified pre-commit gate for the whole monorepo (P7).
# Runs ruff check + format + the pytest suite for every package, then
# stages and commits. Mirrors apps/news/scripts/auto_commit.sh but covers
# news-engine, the TTS gateway and the shared tts_contract lib.
#
# Usage: ./scripts/auto_commit.sh "feat(scope): description"
#        TESTS="apps/news/tests/unit/test_x.py apps/gateway/tests/test_y.py" \
#            ./scripts/auto_commit.sh "test(scope): description"
#
# TESTS (optional) selects an incremental gate: only the listed test files
# run (pytest-xdist parallel, `-n auto`) instead of the full suites. Use it
# for small changes; the full CI (.github/workflows/ci.yml) still runs every
# test on push. Paths may be repo-relative (apps/news/..., apps/gateway/...)
# or relative to the app that owns them.
#
# Staging is limited to already-tracked files (`git add -u`): untracked files
# are reported and left alone, so stray notes or scratch files never ride
# along a commit.

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
    elif [[ -x ".venv/bin/${tool}" ]]; then
        # Workspace checkout installed with `uv sync`: one .venv at the root.
        echo ".venv/bin/${tool}"
    else
        echo "${tool}"
    fi
}

RUF_NEWS="${NEWS_RUF:-$(_bin apps/news ruff)}"
RUF_GW="${GW_RUF:-$(_bin apps/gateway ruff)}"
PYTEST_NEWS="${NEWS_PYTEST:-$(_bin apps/news pytest)}"
PYTEST_GW="${GW_PYTEST:-$(_bin apps/gateway pytest)}"
CONTRACT_PY="${CONTRACT_PY:-python3}"

# Route each TESTS entry to the app that owns it.
NEWS_TEST_PATHS=()
GW_TEST_PATHS=()
if [[ -n "${TESTS:-}" ]]; then
    for path in ${TESTS}; do
        case "$path" in
            apps/news/*) NEWS_TEST_PATHS+=("${path#apps/news/}") ;;
            apps/gateway/*) GW_TEST_PATHS+=("${path#apps/gateway/}") ;;
            *) NEWS_TEST_PATHS+=("$path") ;;
        esac
    done
fi

echo "==> ruff check (news, gateway, tts_contract, scripts)..."
"$RUF_NEWS" check apps/news
"$RUF_GW" check apps/gateway
"$RUF_GW" check libs/tts_contract
"$RUF_NEWS" check scripts

echo "==> ruff format (news, gateway, tts_contract, scripts)..."
"$RUF_NEWS" format apps/news
"$RUF_GW" format apps/gateway
"$RUF_GW" format libs/tts_contract
"$RUF_NEWS" format scripts

if [[ -n "${TESTS:-}" ]]; then
    echo "==> pytest (incremental: ${TESTS})..."
    ((${#NEWS_TEST_PATHS[@]} == 0)) || \
        (cd apps/news && PYTHONPATH=".:../../libs/tts_contract" "$PYTEST_NEWS" -q -n auto "${NEWS_TEST_PATHS[@]}")
    ((${#GW_TEST_PATHS[@]} == 0)) || \
        (cd apps/gateway && PYTHONPATH=".:../../libs/tts_contract" "$PYTEST_GW" -q -n auto "${GW_TEST_PATHS[@]}")
else
    echo "==> pytest (news, full suite, -n auto)..."
    (cd apps/news && PYTHONPATH=".:../../libs/tts_contract" "$PYTEST_NEWS" -q -n auto)

    echo "==> pytest (gateway, full suite, -n auto)..."
    (cd apps/gateway && PYTHONPATH=".:../../libs/tts_contract" "$PYTEST_GW" -q -n auto)
fi

echo "==> Staging tracked changes..."
git add -u
UNTRACKED="$(git ls-files --others --exclude-standard)"
if [[ -n "$UNTRACKED" ]]; then
    echo "==> Untracked files left alone (stage them yourself if intended):"
    echo "$UNTRACKED" | sed 's/^/    /'
fi

echo "==> Committing with message: $MSG"
git commit -m "$MSG"

echo "==> Done."
