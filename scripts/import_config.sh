#!/usr/bin/env bash
set -euo pipefail

# import_config.sh — restore newsvoice configuration, schedules, voices, and
# history from a tarball created by export_config.sh.
#
# Usage:
#   ./scripts/import_config.sh <tarball> [--dry-run] [--wipe] [--with-env]
#
# Options:
#   --dry-run   Show what would change without making any modifications.
#   --wipe      Delete all managed resources before importing (voices, gateway
#               storage, schedules, history).
#   --with-env  Apply .env + collections.toml from the archive (default: only
#               report diffs).

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

# archive-name : destination-path (the config files use distinct names so the
# three .env files no longer collide — the pre-v2 archives stored them all as
# config/.env and could only ever restore one).
CONFIG_MAP=(
    "apps-news.env:apps/news/.env"
    "apps-gateway.env:apps/gateway/.env"
    "deploy.env:deploy/.env"
    "collections.toml:apps/news/collections.toml"
)

GATEWAY_STORAGE="apps/gateway/storage"

# ---------------------------------------------------------------------------
# Helpers
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
    for entry in "${CONFIG_MAP[@]}"; do
        name="${entry%%:*}"
        dest="${entry#*:}"
        archive_file="${ARCHIVE_DIR}/config/${name}"
        if [[ -f "$archive_file" ]]; then
            echo ">>> $dest"
            diff_file "$archive_file" "$dest"
        else
            echo ">>> $dest (not in archive)"
        fi
    done

    echo ""
    echo "--- Voice catalog (modern) ---"
    if [[ -d "${ARCHIVE_DIR}/voices/voice_catalog" ]]; then
        archive_files=$(find "${ARCHIVE_DIR}/voices/voice_catalog" -type f 2>/dev/null | wc -l)
        local_files=0
        [[ -d "${GATEWAY_STORAGE}/voice_catalog" ]] && \
            local_files=$(find "${GATEWAY_STORAGE}/voice_catalog" -type f 2>/dev/null | wc -l)
        echo "Archive: ${archive_files} files, Local: ${local_files} files"
    else
        echo "  (no voice_catalog in archive)"
    fi

    echo ""
    echo "--- Gateway storage (legacy) ---"
    if [[ -d "${ARCHIVE_DIR}/voices/gateway" ]]; then
        for artifact in omnivoice.db melref.wav; do
            if [[ -f "${ARCHIVE_DIR}/voices/gateway/${artifact}" ]]; then
                if [[ -f "${GATEWAY_STORAGE}/${artifact}" ]]; then
                    echo "  ${artifact}: would overwrite local"
                else
                    echo "  ${artifact}: would restore (local missing)"
                fi
            fi
        done
        if [[ -d "${ARCHIVE_DIR}/voices/gateway/voices" ]]; then
            archive_files=$(find "${ARCHIVE_DIR}/voices/gateway/voices" -type f 2>/dev/null | wc -l)
            local_files=0
            [[ -d "${GATEWAY_STORAGE}/voices" ]] && \
                local_files=$(find "${GATEWAY_STORAGE}/voices" -type f 2>/dev/null | wc -l)
            echo "  voices/: Archive ${archive_files} files, Local ${local_files} files"
        fi
    else
        echo "  (no legacy gateway storage in archive)"
    fi

    if [[ -f "${ARCHIVE_DIR}/voices/voice_profiles.json" ]]; then
        archive_sum=$(jq -r '.voices.voice_profiles_checksum // ""' "${MANIFEST}")
        local_sum=""
        [[ -f "apps/gateway/voice_profiles.json" ]] && \
            local_sum=$(sha256sum apps/gateway/voice_profiles.json | cut -d' ' -f1)
        if [[ "${archive_sum}" != "${local_sum}" ]]; then
            echo "  -> voice_profiles.json differs (archive: ${archive_sum}, local: ${local_sum})"
        fi
    fi

    echo ""
    echo "--- Temporal schedules ---"
    command -v tctl >/dev/null 2>&1 && \
        echo "  (would fetch current schedules and compare with archive)" || \
        echo "  tctl not available, cannot compare schedules"

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

    if [[ -d "${GATEWAY_STORAGE}/voice_catalog" ]]; then
        rm -rf "${GATEWAY_STORAGE}/voice_catalog"
        echo "   Removed ${GATEWAY_STORAGE}/voice_catalog"
    fi

    for artifact in omnivoice.db melref.wav voices; do
        if [[ -e "${GATEWAY_STORAGE}/${artifact}" ]]; then
            rm -rf "${GATEWAY_STORAGE:?}/${artifact}"
            echo "   Removed ${GATEWAY_STORAGE}/${artifact}"
        fi
    done

    if command -v tctl >/dev/null 2>&1; then
        echo "   Deleting Temporal schedules matching 'digest-pipeline-*'..."
        tctl --address "${TEMPORAL_ADDRESS:-localhost:7233}" schedule list --output json 2>/dev/null | \
            jq -r '.schedules[].id' | grep '^digest-pipeline-' | while read -r sid; do
            tctl --address "${TEMPORAL_ADDRESS:-localhost:7233}" schedule delete --schedule-id "$sid" 2>/dev/null && \
                echo "     Deleted $sid"
        done || true
    else
        echo "   WARNING: tctl not available, skipping schedule deletion"
    fi

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
for entry in "${CONFIG_MAP[@]}"; do
    name="${entry%%:*}"
    dest="${entry#*:}"
    archive_file="${ARCHIVE_DIR}/config/${name}"
    if [[ ! -f "$archive_file" ]]; then
        echo "   WARNING: config/${name} not in archive, skipping ${dest}"
        continue
    fi
    if [[ "${WITH_ENV}" == "true" ]]; then
        backup_file "$dest"
        mkdir -p "$(dirname "$dest")"
        cp "$archive_file" "$dest"
        echo "   Applied $dest"
    else
        echo "   Diff for $dest (use --with-env to apply):"
        diff_file "$archive_file" "$dest"
    fi
done

# ---------------------------------------------------------------------------
# 4. Restore voices (modern catalog + legacy gateway storage)
# ---------------------------------------------------------------------------
echo ""
echo "=== Restoring voices ==="
if [[ -d "${ARCHIVE_DIR}/voices/voice_catalog" ]]; then
    mkdir -p "${GATEWAY_STORAGE}/voice_catalog"
    rsync -a --delete "${ARCHIVE_DIR}/voices/voice_catalog/" "${GATEWAY_STORAGE}/voice_catalog/"
    echo "   Synced voice_catalog"
fi

if [[ -d "${ARCHIVE_DIR}/voices/gateway" ]]; then
    mkdir -p "${GATEWAY_STORAGE}"
    for artifact in omnivoice.db melref.wav; do
        if [[ -f "${ARCHIVE_DIR}/voices/gateway/${artifact}" ]]; then
            backup_file "${GATEWAY_STORAGE}/${artifact}"
            cp "${ARCHIVE_DIR}/voices/gateway/${artifact}" "${GATEWAY_STORAGE}/${artifact}"
            echo "   Restored ${GATEWAY_STORAGE}/${artifact}"
        fi
    done
    if [[ -d "${ARCHIVE_DIR}/voices/gateway/voices" ]]; then
        mkdir -p "${GATEWAY_STORAGE}/voices"
        rsync -a --delete "${ARCHIVE_DIR}/voices/gateway/voices/" "${GATEWAY_STORAGE}/voices/"
        echo "   Synced gateway voices/"
    fi
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
    mkdir -p apps/news/data
    backup_file apps/news/data/news.db
    cp "${ARCHIVE_DIR}/history/news.db" apps/news/data/news.db
    echo "   Restored news.db"
fi

# ---------------------------------------------------------------------------
# 6. Re-register Temporal schedules from collections.toml
# ---------------------------------------------------------------------------
echo ""
echo "=== Re-registering Temporal schedules ==="
if [[ -f "apps/news/collections.toml" ]] && \
   docker compose -f deploy/docker-compose.yml ps worker 2>/dev/null | grep -q "Up"; then
    docker compose -f deploy/docker-compose.yml exec -T worker \
        python -m src.workers.schedule 2>&1 | head -20
    echo "   Schedules registered via worker"
else
    echo "   WARNING: worker container not running, skipping schedule registration"
    echo "   Run: docker compose exec worker python -m src.workers.schedule"
fi

echo ""
echo "=== Import complete ==="
echo "Next steps:"
echo "  - Review any diffs shown above (run with --with-env to apply .env changes)"
echo "  - Restart services: make down && make up"
echo "  - Verify schedules: make schedule"
