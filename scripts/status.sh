#!/usr/bin/env bash
#
# status.sh — check the health of a deployed news-engine stack.
#
# Checks (in order):
#   1. Docker Compose services are running (no restart loops)
#   2. FastAPI /health endpoint
#   3. Temporal server is reachable (HTTP + gRPC via UI namespaces API)
#   4. Temporal worker is up (log signals) and registered its workflows
#   5. Temporal schedules are registered (if collections are configured)
#   6. External LLM backend connectivity
#   7. External TTS backend connectivity
#   8. Telegram bot token validity
#   9. SQLite data freshness (articles / clusters / deliveries)
#
# Configuration is read from .env (same file used by docker compose) and can
# be overridden per-run with environment variables, e.g.:
#   API_PORT=9000 ./scripts/status.sh
#
# Exit code: 0 if no failures, 2 if any check failed.

set -uo pipefail

RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[0;33m'
CYAN=$'\033[0;36m'
BOLD=$'\033[1m'
RESET=$'\033[0m'

FAILED=0
WARNED=0
declare -a FAILURES=()
declare -a WARNINGS=()

# ---------------------------------------------------------------------------
# Load .env. The file may contain Python-style arrays and comments; only the
# keys we need are picked out, so the rest is ignored safely.
# ---------------------------------------------------------------------------
ENV_FILE="${ENV_FILE:-.env}"
if [[ -f "$ENV_FILE" ]]; then
    _get() {
        local key="$1" default="$2" val=""
        val="$(grep -E "^${key}=" "$ENV_FILE" | tail -n1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
        echo "${val:-$default}"
    }
else
    _get() { echo "$2"; }
fi

export API_PORT="${API_PORT:-$(_get API_PORT 8000)}"
export LLM_API_BASE="${LLM_API_BASE:-$(_get LLM_API_BASE http://192.168.5.4:9000/)}"
export TTS_API_BASE_URL="${TTS_API_BASE_URL:-$(_get TTS_API_BASE_URL http://debwk.elver-exponential.ts.net:8000/api/v1)}"
export TEMPORAL_UI_URL="${TEMPORAL_UI_URL:-http://localhost:8080}"
export DATABASE_PATH="${DATABASE_PATH:-$(_get DATABASE_PATH data/news.db)}"
export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-$(_get TELEGRAM_BOT_TOKEN '')}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ok() { printf "${GREEN}%s OK${RESET}   %s\n" "●" "$*"; }
fail() { printf "${RED}%s FAIL${RESET} %s\n" "●" "$*"; FAILED=1; FAILURES+=("$*"); }
warn() { printf "${YELLOW}%s WARN${RESET} %s\n" "●" "$*"; WARNED=1; WARNINGS+=("$*"); }
info() { printf "${CYAN}%s INFO${RESET} %s\n" "•" "$*"; }
section() { printf "\n${BOLD}== %s ==${RESET}\n" "$1"; }

# hit <url> [curl args…] → prints body; non-zero exit if request failed
hit() {
    local url="$1"
    shift
    curl -sf --connect-timeout 5 --max-time 10 "$@" "$url" 2>/dev/null
}

# valid_json <string> → 0 if the string parses as JSON
valid_json() {
    python3 -c 'import json,sys; json.loads(sys.stdin.read())' <<<"$1" 2>/dev/null
}

section "Environment"
info "API_PORT        = ${API_PORT}"
info "LLM_API_BASE    = ${LLM_API_BASE}"
info "TTS_API_BASE_URL= ${TTS_API_BASE_URL}"
info "TEMPORAL_UI_URL = ${TEMPORAL_UI_URL}"
info "DATABASE_PATH   = ${DATABASE_PATH}"
if [[ -z "$TELEGRAM_BOT_TOKEN" || "$TELEGRAM_BOT_TOKEN" == *your_telegram* ]]; then
    warn "TELEGRAM_BOT_TOKEN is empty or a placeholder (Telegram check skipped)"
fi

# ---------------------------------------------------------------------------
# 1. Docker Compose services
# ---------------------------------------------------------------------------
section "1. Docker Compose services"
SERVICES=(api worker narration-worker temporal temporal-ui temporal-postgres)
if ! command -v docker >/dev/null 2>&1; then
    fail "docker binary not found"
else
    for svc in "${SERVICES[@]}"; do
        state="$(docker compose ps --format '{{.State}}' "$svc" 2>/dev/null | tr -d '[:space:]')"
        restarts="$(docker inspect -f '{{.RestartCount}}' "news_engine_${svc}" 2>/dev/null | tr -d '[:space:]')"
        if [[ "$state" == "running" ]]; then
            ok "container news_engine_${svc} is running${restarts:+ (restarts: $restarts)}"
        elif [[ -z "$state" ]]; then
            fail "container news_engine_${svc} not found (is the stack deployed?)"
        else
            fail "container news_engine_${svc} is in state '$state'"
        fi
        if [[ -n "${restarts}" && "${restarts}" -gt 3 ]]; then
            warn "news_engine_${svc} has restarted ${restarts} times — check logs"
        fi
    done
fi

# ---------------------------------------------------------------------------
# 2. FastAPI health
# ---------------------------------------------------------------------------
section "2. FastAPI health"
HEALTH_URL="http://localhost:${API_PORT}/health"
BODY="$(hit "$HEALTH_URL"; echo "__rc=$?")"
if [[ "${BODY}" == *'"status":"ok"'* ]] || [[ "${BODY}" == *'"status": "ok"'* ]]; then
    ok "GET ${HEALTH_URL} -> ${BODY%__rc=*}"
else
    fail "GET ${HEALTH_URL} (got '${BODY%__rc=*}')"
fi

# ---------------------------------------------------------------------------
# 3. Temporal server + UI
# ---------------------------------------------------------------------------
section "3. Temporal server"
if [[ -n "$(hit "${TEMPORAL_UI_URL}/api/namespaces" 2>/dev/null)" ]]; then
    ok "Temporal namespaces API reachable (${TEMPORAL_UI_URL})"
else
    fail "Temporal namespaces API not reachable (${TEMPORAL_UI_URL})"
fi

if docker compose exec -T temporal bash -c 'exit 0' >/dev/null 2>&1; then
    ok "temporal container reachable for gRPC checks"
else
    fail "temporal container not reachable via docker compose exec"
fi

# ---------------------------------------------------------------------------
# 4. Worker status (log-derived — no health endpoint exists)
# ---------------------------------------------------------------------------
section "4. Temporal workers"
WORKER_SIGS="$(docker compose logs worker --tail 200 2>/dev/null | grep -c 'worker_running')"
if [[ "${WORKER_SIGS:-0}" -ge 1 ]]; then
    ok "worker reached 'worker_running' state"
else
    fail "worker has not logged 'worker_running' in the last 200 lines"
fi
WORKER_ERRS="$(docker compose logs worker --tail 200 2>/dev/null | grep -icE '\b(error|exception|traceback)\b')"
if [[ "${WORKER_ERRS:-0}" -gt 0 ]]; then
    warn "${WORKER_ERRS} error/exception line(s) in worker logs (tail 200)"
else
    ok "no error/exception lines in worker log tail"
fi

NARR_WORKER_SIGS="$(docker compose logs narration-worker --tail 200 2>/dev/null | grep -c 'worker_running')"
if [[ "${NARR_WORKER_SIGS:-0}" -ge 1 ]]; then
    ok "narration-worker reached 'worker_running' state"
else
    fail "narration-worker has not logged 'worker_running' in the last 200 lines"
fi
NARR_WORKER_ERRS="$(docker compose logs narration-worker --tail 200 2>/dev/null | grep -icE '\b(error|exception|traceback)\b')"
if [[ "${NARR_WORKER_ERRS:-0}" -gt 0 ]]; then
    warn "${NARR_WORKER_ERRS} error/exception line(s) in narration-worker logs (tail 200)"
else
    ok "no error/exception lines in narration-worker log tail"
fi

# ---------------------------------------------------------------------------
# 5. Schedules
# ---------------------------------------------------------------------------
section "5. Temporal schedules"
SCHED_COUNT="$(docker compose exec -T temporal \
    temporal operator schedule list --namespace default 2>/dev/null | grep -c 'daily-digest' || true)"
if [[ "${SCHED_COUNT:-0}" -ge 1 ]]; then
    ok "${SCHED_COUNT} schedule(s) registered ('daily-digest-*')"
    docker compose exec -T temporal temporal operator schedule list --namespace default 2>/dev/null
elif [[ -z "$(docker compose exec -T temporal temporal operator schedule list --namespace default 2>&1 | tr -d '[:space:]')" ]]; then
    warn "no schedules listed (run: python -m src.workers.schedule)"
else
    warn "no 'daily-digest-*' schedules found (run: python -m src.workers.schedule)"
fi

# ---------------------------------------------------------------------------
# 6. External LLM backend
# ---------------------------------------------------------------------------
section "6. LLM backend"
case "${LLM_API_BASE%/}" in
    */v1) LLM_MODELS_URL="${LLM_API_BASE%/}/models" ;;
    *)    LLM_MODELS_URL="${LLM_API_BASE%/}/v1/models" ;;
esac
LLM_BODY="$(hit "$LLM_MODELS_URL")"
if [[ -n "$LLM_BODY" ]] && valid_json "$LLM_BODY"; then
    ok "GET ${LLM_MODELS_URL} (valid JSON)"
else
    fail "GET ${LLM_MODELS_URL} unreachable or not JSON ('${LLM_BODY:0:60}')"
fi

# ---------------------------------------------------------------------------
# 7. External TTS backend (OmniVoice)
# ---------------------------------------------------------------------------
section "7. TTS backend"
TTS_HEALTH="${TTS_API_BASE_URL%/}/health"
TTS_BODY="$(hit "$TTS_HEALTH")"
if [[ -n "$TTS_BODY" ]] && valid_json "$TTS_BODY"; then
    ok "GET ${TTS_HEALTH} (valid JSON)"
else
    fail "GET ${TTS_HEALTH} unreachable or not JSON ('${TTS_BODY:0:60}')"
fi

# ---------------------------------------------------------------------------
# 8. Telegram
# ---------------------------------------------------------------------------
section "8. Telegram"
if [[ -z "$TELEGRAM_BOT_TOKEN" || "$TELEGRAM_BOT_TOKEN" == *your_telegram* || "$TELEGRAM_BOT_TOKEN" == *test_bot_token* ]]; then
    warn "TELEGRAM_BOT_TOKEN is empty or a placeholder — delivery check skipped (expected locally)"
else
    TG="$(hit "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe")"
    if [[ "$TG" == *'"ok":true'* ]]; then
        ok "bot token valid (getMe ok)"
    else
        fail "bot token rejected by Telegram (getMe failed)"
    fi
fi

# ---------------------------------------------------------------------------
# 9. Database
# ---------------------------------------------------------------------------
section "9. SQLite database"
DB_REPORT="$(docker compose exec -T api python -c '
import sqlite3
from pathlib import Path
import sys

path = Path("'${DATABASE_PATH}'")
if not path.exists():
    print("MISSING")
    sys.exit(0)

con = sqlite3.connect(path)
cur = con.cursor()

def q(sql):
    try:
        row = cur.execute(sql).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None

print("articles:", q("SELECT COUNT(*) FROM articles"))
print("clusters:", q("SELECT COUNT(*) FROM clusters"))
print("deliveries:", q("SELECT COUNT(*) FROM deliveries"))
print("latest_fetched:", q("SELECT MAX(fetched_at) FROM articles"))
print("latest_delivered:", q("SELECT MAX(delivered_at) FROM deliveries"))
con.close()
' 2>&1)"

echo "${DB_REPORT}" | sed 's/^/     /'
if echo "${DB_REPORT}" | grep -q "MISSING"; then
    fail "database file '${DATABASE_PATH}' not found in api container"
elif echo "${DB_REPORT}" | grep -q "articles:"; then
    ok "database readable ('${DATABASE_PATH}')"
    if [[ "${DB_REPORT}" == *"latest_delivered: None"* ]]; then
        warn "no deliveries recorded yet (never delivered a digest?)"
    fi
else
    fail "could not read database (report: ${DB_REPORT:-empty})"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
section "Summary"
if [[ "${FAILED}" -eq 1 ]]; then
    printf "${RED}%d FAILED check(s):${RESET}\n" "${#FAILURES[@]}"
    for f in "${FAILURES[@]}"; do printf "  - %s\n" "$f"; done
else
    printf "${GREEN}All critical checks passed.${RESET}\n"
fi
if [[ "${WARNED}" -eq 1 ]]; then
    printf "${YELLOW}%d warning(s):${RESET}\n" "${#WARNINGS[@]}"
    for w in "${WARNINGS[@]}"; do printf "  - %s\n" "$w"; done
fi

exit "${FAILED}"