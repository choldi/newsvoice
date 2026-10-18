#!/usr/bin/env bash
set -euo pipefail

# import_config.sh — restore newsvoice configuration, schedules, voices, and history
# from a tarball created by export_config.sh.
#
# Usage:
#   ./scripts/import_config.sh <tarball> [--dry-run] [--wipe] [--with-env]
#
# Options:
#   --dry-run   Show what would change without making any modifications.
#   --wipe      Delete all managed resources before importing (voices, schedules, history).
#   --with-env  Apply .env files from the archive (default: only report diffs).

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TARBALL="${1:-}"
DRY_RUN=false
WIPE=false
WITH_ENV=false

shift || true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        --wipe) WIPE=true; shift ;;
        --with-env) WITH_ENV=true; shift ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "${TARBALL}" || ! -f "${TARBALL}" ]]; then
    echo "Usage: $0 <tarball> [--dry-run] [--wipe] [--with-env]" >&2
    exit 1
fi

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

echo "==> Extracting ${TARBALL}"
tar -xzf "${TARBALL}" -C "${WORK_DIR}"
ARCHIVE_DIR="${WORK_DIR}/$(basename "${TARBALL}" .tar.gz)"

if [[ ! -f "${ARCHIVE_DIR}/manifest.json" ]]; then
    echo "ERROR: manifest.json not found in archive" >&2
    exit 1
fi

MANIFEST="${ARCHIVE_DIR}/manifest.json"
echo "==> Manifest:"
jq . "${MANIFEST}"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
backup_file() {
    local file="$1"
    if [[ -f "$file" ]]; then
        cp "$file" "${file}.bak-$(date -u +%Y%m%d-%H%M%S)"
        echo "     Backed up $file"
    fi
}

diff_file() {
    local src="$1" dst="$2"
    if [[ -f "$dst" ]]; then
        diff -u "$dst" "$src" || true
    else
        echo "     NEW FILE: $dst"
        cat "$src"
    fi
}

# ---------------------------------------------------------------------------
# 1. DRY-RUN: Inventory what exists vs what's in the archive
# ---------------------------------------------------------------------------
if [[ "${DRY_RUN}" == "true" ]]; then
    echo ""
    echo "=== DRY-RUN: comparing existing vs archive ==="

    echo ""
    echo "--- Configuration files ---"
    for f in deploy/.env apps/news/.env apps/gateway/.env apps/news/collections.toml; do
        archive_file="${ARCHIVE_DIR}/config/$(basename "$f")"
        if [[ -f "$archive_file" ]]; then
            echo ">>> $f"
            diff_file "$archive_file" "$f"
        else
            echo ">>> $f (not in archive)"
        fi
    done

    echo ""
    echo "--- Voice catalog ---"
    if [[ -d "${ARCHIVE_DIR}/voices/voice_catalog" ]]; then
        archive_files=$(find "${ARCHIVE_DIR}/voices/voice_catalog" -type f 2>/dev/null | wc -l)
        local_files=0
        if [[ -d "apps/gateway/storage/voice_catalog" ]]; then
            local_files=$(find apps/gateway/storage/voice_catalog -type f 2>/dev/null | wc -l)
        fi
        echo "Archive: ${archive_files} files, Local: ${local_files} files"
        if [[ $archive_files -ne $local_files ]]; then
            echo "  -> Would sync voice_catalog (${archive_files} vs ${local_files})"
        fi
    fi

    if [[ -f "${ARCHIVE_DIR}/voices/voice_profiles.json" ]]; then
        archive_sum=$(jq -r .voices.voice_profiles_checksum "${MANIFEST}")
        if [[ -f "apps/gateway/voice_profiles.json" ]]; then
            local_sum=$(sha256sum apps/gateway/voice_profiles.json | cut -d' ' -f1)
            if [[ "$archive_sum" != "$local_sum" ]]; then
                echo "  -> voice_profiles.json differs (archive: ${archive_sum}, local: ${local_sum})"
            fi
        else
            echo "  -> voice_profiles.json missing locally, would be restored"
        fi
    fi

    echo ""
    echo "--- Temporal schedules ---"
    if command -v tctl >/dev/null 2>&1; then
        echo "  (would fetch current schedules and compare with archive)"
    else
        echo "  tctl not available, cannot compare schedules"
    fi

    echo ""
    echo "--- Database (news.db) ---"
    if [[ -f "${ARCHIVE_DIR}/history/news.db" ]]; then
        if [[ -f "apps/news/data/news.db" ]]; then
            echo "  Local DB exists, would compare table counts"
        else
            echo "  No local DB, would restore"
        fi
    fi

    echo ""
    echo "=== DRY-RUN complete (no changes made) ==="
    exit 0
fi

# ---------------------------------------------------------------------------
# 2. WIPE: Delete managed resources before import
# ---------------------------------------------------------------------------
if [[ "${WIPE}" == "true" ]]; then
    echo ""
    echo "=== WIPE: deleting managed resources ==="

    # Voice catalog runtime
    if [[ -d "apps/gateway/storage/voice_catalog" ]]; then
        rm -rf apps/gateway/storage/voice_catalog
        echo "   Removed apps/gateway/storage/voice_catalog"
    fi

    # Temporal schedules (digest-pipeline-*)
    if command -v tctl >/dev/null 2>&1; then
        echo "   Deleting Temporal schedules matching 'digest-pipeline-*'..."
        tctl --address "${TEMPORAL_HOST:-localhost:7233}" schedule list --output json 2>/dev/null | \
            jq -r '.schedules[].id' | grep '^digest-pipeline-' | while read -r sid; do
            tctl --address "${TEMPORAL_HOST:-localhost:7233}" schedule delete --schedule-id "$sid" 2>/dev/null && \
                echo "     Deleted $sid"
        done || true
    else
        echo "   WARNING: tctl not available, skipping schedule deletion"
    fi

    # Database history tables
    if [[ -f "apps/news/data/news.db" ]]; then
        echo "   Wiping history tables in news.db..."
        sqlite3 apps/news/data/news.db <<'SQL'
DELETE FROM deliveries;
DELETE FROM digest_runs;
DELETE FROM audio_jobs;
DELETE FROM narration_scripts;
DELETE FROM verifications;
DELETE FROM modal_spend;
DELETE FROM monitor_events;
DELETE FROM tts_leases;
DELETE FROM tts_lease_waiters;
DELETE FROM users;
DELETE FROM login_codes;
DELETE FROM sessions;
DELETE FROM kv_state;
SQL
        echo "   History tables wiped"
    fi
fi

# ---------------------------------------------------------------------------
# 3. Restore configuration files
# ---------------------------------------------------------------------------
echo ""
echo "=== Restoring configuration files ==="
for f in deploy/.env apps/news/.env apps/gateway/.env apps/news/collections.toml; do
    archive_file="${ARCHIVE_DIR}/config/$(basename "$f")"
    if [[ -f "$archive_file" ]]; then
        if [[ "${WITH_ENV}" == "true" ]]; then
            backup_file "$f"
            cp "$archive_file" "$f"
            echo "   Applied $f"
        else
            echo "   Diff for $f (use --with-env to apply):"
            diff_file "$archive_file" "$f"
        fi
    else
        echo "   WARNING: $f not in archive, skipping"
    fi
done

# ---------------------------------------------------------------------------
# 4. Restore voice catalog
# ---------------------------------------------------------------------------
echo ""
echo "=== Restoring voice catalog ==="
if [[ -d "${ARCHIVE_DIR}/voices/voice_catalog" ]]; then
    mkdir -p apps/gateway/storage/voice_catalog
    rsync -a --delete "${ARCHIVE_DIR}/voices/voice_catalog/" apps/gateway/storage/voice_catalog/
    echo "   Synced voice_catalog"
fi

if [[ -f "${ARCHIVE_DIR}/voices/voice_profiles.json" ]]; then
    backup_file apps/gateway/voice_profiles.json
    cp "${ARCHIVE_DIR}/voices/voice_profiles.json" apps/gateway/voice_profiles.json
    echo "   Restored voice_profiles.json"
fi

# ---------------------------------------------------------------------------
# 5. Restore database
# ---------------------------------------------------------------------------
echo ""
echo "=== Restoring database ==="
if [[ -f "${ARCHIVE_DIR}/history/news.db" ]]; then
    backup_file apps/news/data/news.db
    cp "${ARCHIVE_DIR}/history/news.db" apps/news/data/news.db
    echo "   Restored news.db"
fi

# ---------------------------------------------------------------------------
# 6. Re-register Temporal schedules from collections.toml
# ---------------------------------------------------------------------------
echo ""
echo "=== Re-registering Temporal schedules ==="
if command -v tctl >/dev/null 2>&1 && [[ -f "apps/news/collections.toml" ]]; then
    # The schedule registration is done via the worker CLI
    # We need the worker container to be running
    if docker compose -f deploy/docker-compose.yml ps worker 2>/dev/null | grep -q "Up"; then
        docker compose -f deploy/docker-compose.yml exec -T worker \
            python -m src.workers.schedule 2>&1 | head -20
        echo "   Schedules registered via worker"
    else
        echo "   WARNING: worker container not running, skipping schedule registration"
        echo "   Run: docker compose exec worker python -m src.workers.schedule"
    fi
else
    echo "   WARNING: tctl not available or collections.toml missing, skipping"
fi

echo ""
echo "=== Import complete ==="
echo "Next steps:"
echo "  - Review any diffs shown above (run with --with-env to apply .env changes)"
echo "  - Restart services: make down && make up"
echo "  - Verify schedules: make schedule"