#!/usr/bin/env bash
set -euo pipefail

# export_config.sh — create a portable backup of newsvoice configuration,
# Temporal schedules, voice catalog/gateway storage, and digest history.
#
# It understands two source layouts:
#   monorepo  (default when apps/news exists): the unified repo tree.
#   legacy    (default when ~/news-summary or ~/ov_api exists): the pre-merge
#             checkouts, used to seed the P10 cutover.
#
# Usage:
#   ./scripts/export_config.sh [--with-audio] [--output DIR]
#   ./scripts/export_config.sh --source legacy \
#       [--news-root DIR] [--gateway-root DIR] [--with-audio] [--output DIR]
#
# Output: <OUT_DIR>/newsvoice-config[-legacy]-<timestamp>.tar.gz (manifest inside).
#
# Archive layout (import_config.sh reads this):
#   config/deploy.env          compose interpolation vars (generated)
#   config/apps-news.env       news-engine runtime .env
#   config/apps-gateway.env    gateway runtime .env
#   config/collections.toml    news-engine collections
#   schedules/schedules.json   Temporal schedule snapshot
#   voices/voice_catalog/      modern gateway runtime catalog (if present)
#   voices/voice_profiles.json committed catalog (if present)
#   voices/gateway/            gateway runtime storage: omnivoice.db, melref.wav, voices/
#   history/news.db            digest history

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

WITH_AUDIO=false
OUT_DIR="backups"
SOURCE=""
NEWS_ROOT="${NEWS_ROOT:-$HOME/news-summary}"
GATEWAY_ROOT="${GATEWAY_ROOT:-$HOME/ov_api}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-audio) WITH_AUDIO=true; shift ;;
        --output) OUT_DIR="$2"; shift 2 ;;
        --source) SOURCE="$2"; shift 2 ;;
        --news-root) NEWS_ROOT="$2"; shift 2 ;;
        --gateway-root) GATEWAY_ROOT="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# --- Detect the layout unless --source was given -----------------------------
if [[ -z "${SOURCE}" ]]; then
    if [[ -d "apps/news" ]]; then
        SOURCE="monorepo"
    elif [[ -d "${NEWS_ROOT}" || -d "${GATEWAY_ROOT}" ]]; then
        SOURCE="legacy"
    else
        echo "ERROR: cannot detect layout (no apps/news, no legacy roots)." >&2
        echo "       Pass --source monorepo|legacy explicitly." >&2
        exit 1
    fi
fi

case "${SOURCE}" in
    monorepo)
        NEWS_ENV="apps/news/.env"
        GATEWAY_ENV="apps/gateway/.env"
        DEPLOY_ENV="deploy/.env"
        COLLECTIONS="apps/news/collections.toml"
        NEWS_DB="apps/news/data/news.db"
        GATEWAY_STORAGE="apps/gateway/storage"
        ;;
    legacy)
        NEWS_ENV="${NEWS_ROOT}/.env"
        GATEWAY_ENV="${GATEWAY_ROOT}/.env"
        DEPLOY_ENV=""
        COLLECTIONS="${NEWS_ROOT}/collections.toml"
        NEWS_DB="${NEWS_ROOT}/data/news.db"
        GATEWAY_STORAGE="${GATEWAY_ROOT}/storage"
        ;;
    *)
        echo "ERROR: unknown --source '${SOURCE}' (expected monorepo|legacy)" >&2
        exit 1
        ;;
esac

# Normalize OUT_DIR to an absolute path (a relative one is repo-relative).
[[ "${OUT_DIR}" == /* ]] || OUT_DIR="${REPO_ROOT}/${OUT_DIR}"

TS="$(date -u +%Y%m%d-%H%M%S)"
ARCHIVE_NAME="newsvoice-config-${SOURCE}-${TS}"
WORK_DIR="$(mktemp -d)"
ARCHIVE_DIR="${WORK_DIR}/${ARCHIVE_NAME}"
MANIFEST="${ARCHIVE_DIR}/manifest.json"

mkdir -p "${ARCHIVE_DIR}"/{config,schedules,voices,history}
mkdir -p "${OUT_DIR}"

echo "==> Exporting from '${SOURCE}' layout to ${ARCHIVE_DIR}"
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
json_str() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"; }

# Emit a manifest config-file entry, tracking the trailing comma.
_manifest_first=true
manifest_config_entry() {
    local archive_rel="$1" src="$2" checksum size
    [[ -f "$src" ]] || return 0
    checksum="$(sha256sum "$src" | cut -d' ' -f1)"
    size="$(stat -c%s "$src")"
    if [[ "${_manifest_first}" == "true" ]]; then _manifest_first=false; else echo "," >> "${MANIFEST}"; fi
    printf '      {"path": %s, "sha256": "%s", "size": %s}\n' \
        "$(json_str "$archive_rel")" "${checksum}" "${size}" >> "${MANIFEST}"
}

# tctl wrapper: host binary if present, else the temporal container.
_tctl() {
    if command -v tctl >/dev/null 2>&1; then
        command tctl --address "${TEMPORAL_ADDRESS:-localhost:7233}" "$@"
    elif docker ps --format '{{.Names}}' 2>/dev/null | grep -qx news_engine_temporal; then
        docker exec news_engine_temporal tctl --address temporal:7233 "$@"
    else
        return 1
    fi
}

# ---------------------------------------------------------------------------
# 1. Configuration files
# ---------------------------------------------------------------------------
echo "   - Configuration files"
CONFIG_COUNT=0
copy_config() {
    local src="$1" dst_name="$2"
    [[ -f "$src" ]] || return 0
    cp "$src" "${ARCHIVE_DIR}/config/${dst_name}"
    CONFIG_COUNT=$((CONFIG_COUNT + 1))
    echo "     + config/${dst_name}"
}
copy_config "${NEWS_ENV}" "apps-news.env"
copy_config "${GATEWAY_ENV}" "apps-gateway.env"
copy_config "${COLLECTIONS}" "collections.toml"

# deploy.env: reuse the repo's if present, otherwise generate defaults.
if [[ -n "${DEPLOY_ENV}" && -f "${DEPLOY_ENV}" ]]; then
    cp "${DEPLOY_ENV}" "${ARCHIVE_DIR}/config/deploy.env"
else
    cat > "${ARCHIVE_DIR}/config/deploy.env" <<'EOF'
# =============================================================================
# newsvoice — compose-level variables
# =============================================================================
API_PORT=8000
GATEWAY_PORT=8001
TTS_PATH=/opt/omnivoice
COMPOSE_PROFILES=news,gateway
EOF
fi
CONFIG_COUNT=$((CONFIG_COUNT + 1))
echo "     + config/deploy.env"

# ---------------------------------------------------------------------------
# 2. Temporal schedules snapshot
# ---------------------------------------------------------------------------
echo "   - Temporal schedules"
SCHEDULE_COUNT=0
SCHEDULES_RAW="${WORK_DIR}/schedules.raw"
if _tctl schedule list --output json > "${ARCHIVE_DIR}/schedules/schedules.json" 2>/dev/null; then
    SCHEDULE_COUNT=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("schedules", [])))' \
        "${ARCHIVE_DIR}/schedules/schedules.json" 2>/dev/null || echo 0)
elif _tctl schedule list > "${SCHEDULES_RAW}" 2>/dev/null; then
    # tctl builds without --output: parse the fixed-width table.
    cp "${SCHEDULES_RAW}" "${ARCHIVE_DIR}/schedules/schedules.txt"
    python3 - "${SCHEDULES_RAW}" "${ARCHIVE_DIR}/schedules/schedules.json" <<'PY'
import json, re, sys
raw, out = sys.argv[1], sys.argv[2]
schedules = []
for line in open(raw):
    line = line.rstrip("\n")
    if not line.strip() or "DEPRECATION" in line or line.strip().startswith("ScheduleId"):
        continue
    parts = re.split(r"\s{2,}", line.strip())
    if len(parts) >= 2 and not parts[0].startswith("2026/"):
        schedules.append({
            "schedule_id": parts[0],
            "workflow_type": parts[1],
            "paused": (len(parts) > 2 and parts[2].lower() == "true"),
        })
json.dump({"schedules": schedules}, open(out, "w"), indent=2)
PY
    SCHEDULE_COUNT=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("schedules", [])))' \
        "${ARCHIVE_DIR}/schedules/schedules.json" 2>/dev/null || echo 0)
else
    echo "     WARNING: Temporal not reachable, empty schedule snapshot"
    echo '{"schedules": []}' > "${ARCHIVE_DIR}/schedules/schedules.json"
fi
echo "     Captured ${SCHEDULE_COUNT} schedules"

# ---------------------------------------------------------------------------
# 3. Voice catalog (modern) + gateway storage (legacy) + committed catalog
# ---------------------------------------------------------------------------
echo "   - Voices"
VOICE_FILES=0
if [[ -d "${GATEWAY_STORAGE}/voice_catalog" ]]; then
    cp -a "${GATEWAY_STORAGE}/voice_catalog" "${ARCHIVE_DIR}/voices/"
    VOICE_FILES=$(find "${ARCHIVE_DIR}/voices/voice_catalog" -type f 2>/dev/null | wc -l)
    echo "     + voice_catalog (${VOICE_FILES} files)"
fi

GATEWAY_DB_COUNT=0
GATEWAY_VOICE_FILES=0
if [[ -d "${GATEWAY_STORAGE}" ]]; then
    mkdir -p "${ARCHIVE_DIR}/voices/gateway"
    for artifact in omnivoice.db melref.wav; do
        if [[ -f "${GATEWAY_STORAGE}/${artifact}" ]]; then
            cp "${GATEWAY_STORAGE}/${artifact}" "${ARCHIVE_DIR}/voices/gateway/"
            echo "     + gateway/${artifact}"
        fi
    done
    if [[ -d "${GATEWAY_STORAGE}/voices" ]]; then
        cp -a "${GATEWAY_STORAGE}/voices" "${ARCHIVE_DIR}/voices/gateway/"
        GATEWAY_VOICE_FILES=$(find "${ARCHIVE_DIR}/voices/gateway/voices" -type f 2>/dev/null | wc -l)
        echo "     + gateway/voices (${GATEWAY_VOICE_FILES} files)"
    fi
    if [[ -f "${ARCHIVE_DIR}/voices/gateway/omnivoice.db" ]]; then
        GATEWAY_DB_COUNT=$(python3 - "${ARCHIVE_DIR}/voices/gateway/omnivoice.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
print(con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0])
PY
)
    fi
else
    echo "     WARNING: gateway storage '${GATEWAY_STORAGE}' not found"
    mkdir -p "${ARCHIVE_DIR}/voices/gateway"
fi

VP_CHECKSUM=""
VP_SRC=""
[[ -f "apps/gateway/voice_profiles.json" ]] && VP_SRC="apps/gateway/voice_profiles.json"
[[ -f "apps/news/voice_profiles.json" ]] && VP_SRC="apps/news/voice_profiles.json"
if [[ -n "${VP_SRC}" ]]; then
    VP_CHECKSUM="$(sha256sum "${VP_SRC}" | cut -d' ' -f1)"
    cp "${VP_SRC}" "${ARCHIVE_DIR}/voices/voice_profiles.json"
    echo "     + voice_profiles.json (sha256: ${VP_CHECKSUM})"
fi

# ---------------------------------------------------------------------------
# 4. Digest history (SQLite database)
# ---------------------------------------------------------------------------
echo "   - Database (news.db)"
DB_COUNT=0
if [[ -f "${NEWS_DB}" ]]; then
    cp "${NEWS_DB}" "${ARCHIVE_DIR}/history/news.db"
    if [[ "${WITH_AUDIO}" != "true" ]]; then
        python3 - "${ARCHIVE_DIR}/history/news.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
if "digest_runs" in tables:
    cols = {r[1] for r in con.execute("PRAGMA table_info(digest_runs)")}
    for col in ("audio_path", "narration_path"):
        if col in cols:
            con.execute(f"UPDATE digest_runs SET {col} = NULL")
if "audio_jobs" in tables:
    con.execute("DELETE FROM audio_jobs")
con.commit()
con.close()
PY
    fi
    DB_COUNT=$(python3 - "${ARCHIVE_DIR}/history/news.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
print(con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0])
PY
)
    echo "     Exported ${DB_COUNT} tables (with_audio=${WITH_AUDIO})"
else
    echo "     WARNING: ${NEWS_DB} not found"
    touch "${ARCHIVE_DIR}/history/news.db"
fi

# ---------------------------------------------------------------------------
# 5. Generate manifest.json
# ---------------------------------------------------------------------------
echo "   - Generating manifest.json"
cat > "${MANIFEST}" <<EOF
{
  "version": 2,
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "hostname": "$(hostname)",
  "source_layout": "${SOURCE}",
  "config": {
    "file_count": ${CONFIG_COUNT},
    "files": [
EOF
manifest_config_entry "config/apps-news.env" "${NEWS_ENV}"
manifest_config_entry "config/apps-gateway.env" "${GATEWAY_ENV}"
manifest_config_entry "config/collections.toml" "${COLLECTIONS}"
manifest_config_entry "config/deploy.env" "${ARCHIVE_DIR}/config/deploy.env"
cat >> "${MANIFEST}" <<EOF
    ]
  },
  "schedules": {
    "count": ${SCHEDULE_COUNT},
    "file": "schedules/schedules.json"
  },
  "voices": {
    "voice_catalog_files": ${VOICE_FILES},
    "gateway_db_tables": ${GATEWAY_DB_COUNT},
    "gateway_voice_files": ${GATEWAY_VOICE_FILES},
    "voice_profiles_checksum": "${VP_CHECKSUM}"
  },
  "history": {
    "table_count": ${DB_COUNT},
    "file": "history/news.db",
    "with_audio": ${WITH_AUDIO}
  }
}
EOF

# ---------------------------------------------------------------------------
# 6. Create tarball
# ---------------------------------------------------------------------------
echo "   - Creating tarball"
cd "${WORK_DIR}"
tar -czf "${OUT_DIR}/${ARCHIVE_NAME}.tar.gz" "${ARCHIVE_NAME}"
echo "==> Done: ${OUT_DIR}/${ARCHIVE_NAME}.tar.gz"
echo "==> Manifest: ${MANIFEST}"

rm -rf "${WORK_DIR}"
