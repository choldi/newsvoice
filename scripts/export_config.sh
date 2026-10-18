#!/usr/bin/env bash
set -euo pipefail

# export_config.sh — create a portable backup of newsvoice configuration,
# Temporal schedules, voice catalog, and (optionally) digest history/audio.
#
# Usage:
#   ./scripts/export_config.sh [--with-audio] [--output DIR]
#
# Output: backups/newsvoice-config-<timestamp>.tar.gz + manifest.json inside.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

WITH_AUDIO=false
OUT_DIR="backups"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-audio) WITH_AUDIO=true; shift ;;
        --output) OUT_DIR="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

TS="$(date -u +%Y%m%d-%H%M%S)"
ARCHIVE_NAME="newsvoice-config-${TS}"
WORK_DIR="$(mktemp -d)"
ARCHIVE_DIR="${WORK_DIR}/${ARCHIVE_NAME}"
MANIFEST="${ARCHIVE_DIR}/manifest.json"

mkdir -p "${ARCHIVE_DIR}"/{config,schedules,voices,history}
mkdir -p "${OUT_DIR}"

echo "==> Exporting to ${ARCHIVE_DIR}"

# ---------------------------------------------------------------------------
# 1. Configuration files
# ---------------------------------------------------------------------------
echo "   - Configuration files"
CONFIG_COUNT=0
for f in \
    deploy/.env \
    apps/news/.env \
    apps/gateway/.env \
    apps/news/collections.toml
do
    if [[ -f "$f" ]]; then
        cp "$f" "${ARCHIVE_DIR}/config/"
        CONFIG_COUNT=$((CONFIG_COUNT + 1))
    else
        echo "     WARNING: $f not found, skipping"
    fi
done

# ---------------------------------------------------------------------------
# 2. Temporal schedules snapshot
# ---------------------------------------------------------------------------
echo "   - Temporal schedules (if Temporal is reachable)"
SCHEDULE_COUNT=0
if command -v tctl >/dev/null 2>&1; then
    if tctl --address "${TEMPORAL_HOST:-localhost:7233}" schedule list --output json > "${ARCHIVE_DIR}/schedules/schedules.json" 2>/dev/null; then
        SCHEDULE_COUNT=$(jq '.schedules | length' "${ARCHIVE_DIR}/schedules/schedules.json" 2>/dev/null || echo 0)
        echo "     Captured ${SCHEDULE_COUNT} schedules"
    else
        echo "     WARNING: could not fetch schedules (Temporal not reachable?)"
        echo '[]' > "${ARCHIVE_DIR}/schedules/schedules.json"
    fi
else
    echo "     WARNING: tctl not in PATH, skipping schedule snapshot"
    echo '[]' > "${ARCHIVE_DIR}/schedules/schedules.json"
fi

# ---------------------------------------------------------------------------
# 3. Voice catalog (gateway runtime)
# ---------------------------------------------------------------------------
echo "   - Voice catalog"
VOICE_FILES=0
VOICE_CATALOG_SRC="apps/gateway/storage/voice_catalog"
if [[ -d "${VOICE_CATALOG_SRC}" ]]; then
    cp -a "${VOICE_CATALOG_SRC}" "${ARCHIVE_DIR}/voices/"
    VOICE_FILES=$(find "${ARCHIVE_DIR}/voices/voice_catalog" -type f 2>/dev/null | wc -l)
    echo "     Copied ${VOICE_FILES} files from voice_catalog"
else
    echo "     WARNING: ${VOICE_CATALOG_SRC} not found, creating empty dir"
    mkdir -p "${ARCHIVE_DIR}/voices/voice_catalog"
fi

# Include committed voice_profiles.json checksum
VP_CHECKSUM=""
if [[ -f "apps/gateway/voice_profiles.json" ]]; then
    VP_CHECKSUM=$(sha256sum apps/gateway/voice_profiles.json | cut -d' ' -f1)
    cp apps/gateway/voice_profiles.json "${ARCHIVE_DIR}/voices/voice_profiles.json"
    echo "     Included voice_profiles.json (sha256: ${VP_CHECKSUM})"
fi

# ---------------------------------------------------------------------------
# 4. Digest history (SQLite database)
# ---------------------------------------------------------------------------
echo "   - Database (news.db)"
DB_SRC="apps/news/data/news.db"
DB_COUNT=0
if [[ -f "${DB_SRC}" ]]; then
    if [[ "${WITH_AUDIO}" == "true" ]]; then
        cp "${DB_SRC}" "${ARCHIVE_DIR}/history/news.db"
    else
        # Export without audio blobs (audio_path, narration_path) to keep it small
        sqlite3 "${DB_SRC}" ".dump" | \
            sed "/^INSERT INTO.*audio_jobs.*/d;/^INSERT INTO.*digest_runs.*audio_path.*/d;/^INSERT INTO.*digest_runs.*narration_path.*/d" | \
            sqlite3 "${ARCHIVE_DIR}/history/news.db"
    fi
    DB_COUNT=$(sqlite3 "${ARCHIVE_DIR}/history/news.db" "SELECT COUNT(*) FROM sqlite_master WHERE type='table';" 2>/dev/null || echo 0)
    echo "     Exported ${DB_COUNT} tables"
else
    echo "     WARNING: ${DB_SRC} not found"
    touch "${ARCHIVE_DIR}/history/news.db"
fi

# ---------------------------------------------------------------------------
# 5. Generate manifest.json
# ---------------------------------------------------------------------------
echo "   - Generating manifest.json"
cat > "${MANIFEST}" <<EOF
{
  "version": 1,
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "hostname": "$(hostname)",
  "config": {
    "file_count": ${CONFIG_COUNT},
    "files": [
EOF

# Add config file checksums
first=true
for f in deploy/.env apps/news/.env apps/gateway/.env apps/news/collections.toml; do
    if [[ -f "$f" ]]; then
        if [[ "$first" == "true" ]]; then first=false; else echo "," >> "${MANIFEST}"; fi
        checksum=$(sha256sum "$f" | cut -d' ' -f1)
        size=$(stat -c%s "$f" 2>/dev/null || stat -f%z "$f")
        cat >> "${MANIFEST}" <<E
      {"path": "$f", "sha256": "${checksum}", "size": ${size}}
E
    fi
done

cat >> "${MANIFEST}" <<EOF
    ]
  },
  "schedules": {
    "count": ${SCHEDULE_COUNT},
    "file": "schedules/schedules.json"
  },
  "voices": {
    "file_count": ${VOICE_FILES},
    "voice_profiles_checksum": "${VP_CHECKSUM}",
    "directory": "voices/voice_catalog"
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
tar -czf "${REPO_ROOT}/${OUT_DIR}/${ARCHIVE_NAME}.tar.gz" "${ARCHIVE_NAME}"
echo "==> Done: ${OUT_DIR}/${ARCHIVE_NAME}.tar.gz"
echo "==> Manifest: ${MANIFEST}"

# Cleanup
rm -rf "${WORK_DIR}"