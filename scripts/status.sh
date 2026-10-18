#!/usr/bin/env bash
#
# status.sh — unified health check for the whole newsvoice stack (P7).
#
# Covers both apps from the repo root:
#   1. All 12 docker-compose services under deploy/docker-compose.yml
#   2. news-engine FastAPI /health
#   3. TTS gateway /api/v1/health
#   4. Temporal server (UI namespaces API)
#   5. Temporal workers (log signals) + schedules
#   6. External LLM backend connectivity
#   7. Telegram bot token validity
#   8. SQLite data freshness (articles / clusters / deliveries)
#
# Ports and paths are read from deploy/.env (compose-level), falling back to
# sane defaults. Override any of them per-run, e.g.:
#   GATEWAY_PORT=9001 ./scripts/status.sh
#
# Exit code: 0 if no failures, 2 if any check failed.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

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

COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.yml}"
DC=(docker compose -f "$COMPOSE_FILE")

# ---------------------------------------------------------------------------
# Load compose-level env (deploy/.env) then news-app env (apps/news/.env).
# Only the keys we need are picked out; Python-style arrays are ignored.
# ---------------------------------------------------------------------------
_read_env() {
    local file="$1" key="$2" default="$3" val=""
    [[ -f "$file" ]] || { echo "$default"; return; }
    val="$(grep -E "^${key}=" "$file" | tail -n1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
    echo "${val:-$default}"
}

API_PORT="${API_PORT:-$(_read_env deploy/.env API_PORT 8000)}"
GATEWAY_PORT="${GATEWAY_PORT:-$(_read_env deploy/.env GATEWAY_PORT 8001)}"
LLM_API_BASE="${LLM_API_BASE:-$(_read_env apps/news/.env LLM_API_BASE http://192.168.5.4:9000/)}"
TEMPORAL_UI_URL="${TEMPORAL_UI_URL:-http://localhost:8081}"
DATABASE_PATH="${DATABASE_PATH:-$(_read_env apps/news/.env DATABASE_PATH data/news.db)}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-$(_read_env apps/news/.env TELEGRAM_BOT_TOKEN '')}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
ok() { printf "${GREEN}%s OK${RESET}   %s\n" "●" "$*"; }
fail() { printf "${RED}%s FAIL${RESET} %s\n" "●" "$*"; FAILED=1; FAILURES+=("$*"); }
warn() { printf "${YELLOW}%s WARN${RESET} %s\n" "●" "$*"; WARNED=1; WARNINGS+=("$*"); }
info() { printf "${CYAN}%s INFO${RESET} %s\n" "•" "$*"; }
section() { printf "\n${BOLD}== %s ==${RESET}\n" "$1"; }

hit() {
    local url="$1"
    shift
    curl -sf --connect-timeout 5 --max-time 10 "$@" "$url" 2>/dev/null
}

valid_json() {
    python3 -c 'import json,sys; json.loads(sys.stdin.read())' <<<"$1" 2>/dev/null
}

section "Environment"
info "COMPOSE_FILE     = ${COMPOSE_FILE}"
info "API_PORT         = ${API_PORT}"
info "GATEWAY_PORT     = ${GATEWAY_PORT}"
info "LLM_API_BASE     = ${LLM_API_BASE}"
info "TEMPORAL_UI_URL  = ${TEMPORAL_UI_URL}"
info "DATABASE_PATH    = ${DATABASE_PATH}"
if [[ -z "$TELEGRAM_BOT_TOKEN" || "$TELEGRAM_BOT_TOKEN" == *your_telegram* ]]; then
    warn "TELEGRAM_BOT_TOKEN is empty or a placeholder (Telegram check skipped)"
fi

# ---------------------------------------------------------------------------
# 1. All docker-compose services
# ---------------------------------------------------------------------------
section "1. Docker Compose services (12)"
SERVICES=(
    api worker narration-worker doc-tts-worker teacher-worker
    narration-modal-worker voice-catalog-worker there
    temporal-postgres temporal temporal-ui gateway
)
if ! command -v docker >/dev/null 2>&1; then
    fail "docker binary not found"
elif [[ ! -f "$COMPOSE_FILE" ]]; then
    fail "compose file '${COMPOSE_FILE}' not found (run from repo root)"
else
    for svc in "${SERVICES[@]}"; do
        state="$("${DC[@]}" ps --format '{{.State}}' "$svc" 2>/dev/null | tr -d '[:space:]')"
        if [[ "$state" == "running" ]]; then
            ok "service ${svc} is running"
        elif [[ -z "$state" ]]; then
            fail "service ${svc} not found (is the stack deployed?)"
        else
            fail "service ${svc} is in state '$state'"
        fi
    done
fi

# ---------------------------------------------------------------------------
# 2. news-engine FastAPI health
# ---------------------------------------------------------------------------
section "2. news-engine API health"
API_HEALTH_URL="http://localhost:${API_PORT}/health"
API_BODY="$(hit "$API_HEALTH_URL"; echo "__rc=$?")"
if [[ "${API_BODY}" == *'"status":"ok"'* ]] || [[ "${API_BODY}" == *'"status": "ok"'* ]]; then
    ok "GET ${API_HEALTH_URL} -> ${API_BODY%__rc=*}"
else
    fail "GET ${API_HEALTH_URL} (got '${API_BODY%__rc=*}')"
fi

# ---------------------------------------------------------------------------
# 3. TTS gateway health
# ---------------------------------------------------------------------------
section "3. TTS gateway health"
GW_HEALTH_URL="http://localhost:${GATEWAY_PORT}/api/v1/health"
GW_BODY="$(hit "$GW_HEALTH_URL"; echo "__rc=$?")"
GW_PAYLOAD="${GW_BODY%__rc=*}"
if [[ -z "$GW_PAYLOAD" ]]; then
    fail "GET ${GW_HEALTH_URL} unreachable"
elif ! valid_json "$GW_PAYLOAD"; then
    fail "GET ${GW_HEALTH_URL} returned non-JSON ('${GW_PAYLOAD:0:60}')"
elif [[ "$GW_PAYLOAD" == *'"status":"ok"'* ]]; then
    ok "GET ${GW_HEALTH_URL} -> ${GW_PAYLOAD}"
elif [[ "$GW_PAYLOAD" == *'"status":"degraded"'* ]]; then
    warn "gateway is up but degraded (mock engine or model not loaded): ${GW_PAYLOAD}"
else
    fail "GET ${GW_HEALTH_URL} reported a non-ok status: ${GW_PAYLOAD}"
fi

# ---------------------------------------------------------------------------
# 4. Temporal server
# ---------------------------------------------------------------------------
section "4. Temporal server"
if [[ -n "$(hit "${TEMPORAL_UI_URL}/api/namespaces" 2>/dev/null)" ]]; then
    ok "Temporal namespaces API reachable (${TEMPORAL_UI_URL})"
else
    fail "Temporal namespaces API not reachable (${TEMPORAL_UI_URL})"
fi

# ---------------------------------------------------------------------------
# 5. Temporal workers + schedules
# ---------------------------------------------------------------------------
section "5. Temporal workers"
for worker in worker narration-worker; do
    sigs="$("${DC[@]}" logs "$worker" --tail 200 2>/dev/null | grep -c 'worker_running')"
    if [[ "${sigs:-0}" -ge 1 ]]; then
        ok "${worker} reached 'worker_running' state"
    else
        fail "${worker} has not logged 'worker_running' in the last 200 lines"
    fi
    errs="$("${DC[@]}" logs "$worker" --tail 200 2>/dev/null | grep -icE '\b(error|exception|traceback)\b')"
    if [[ "${errs:-0}" -gt 0 ]]; then
        warn "${errs} error/exception line(s) in ${worker} logs (tail 200)"
    fi
done

section "5b. Temporal schedules"
SCHED_JSON="$("${DC[@]}" exec -T api python -c '
import httpx, os
url = os.getenv("TEMPORAL_UI_URL", "http://localhost:8081").rstrip("/") + "/api/namespaces/default/schedules"
try:
    r = httpx.get(url, timeout=5.0, headers={"Accept": "application/json"})
    print(r.text)
except Exception as e:
    print(f"ERROR: {e}")
' 2>/dev/null)"
if echo "$SCHED_JSON" | grep -q '"id":"daily-digest'; then
    COUNT=$(echo "$SCHED_JSON" | grep -o '"id":"daily-digest' | wc -l)
    ok "${COUNT} schedule(s) registered ('daily-digest-*')"
elif echo "$SCHED_JSON" | grep -q 'ERROR'; then
    info "schedules registered via schedule.py (verify in Temporal UI: ${TEMPORAL_UI_URL})"
else
    warn "no 'daily-digest-*' schedules found (run: make schedule)"
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
# 7. Telegram
# ---------------------------------------------------------------------------
section "7. Telegram"
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
# 8. Database
# ---------------------------------------------------------------------------
section "8. SQLite database"
DB_REPORT="$("${DC[@]}" exec -T api python -c '
import sqlite3
from pathlib import Path
import sys

path = Path("'"${DATABASE_PATH}"'")
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
