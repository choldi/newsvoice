#!/usr/bin/env bash
#
# create_namespaces.sh — create the Temporal namespaces news-engine uses.
#
# Every worker connects to its own namespace, so a fresh Temporal (a new
# temporal_pg_data volume, or a restore) comes back with only `default` and the
# workers sit idle forever: no error, no digest. Run this after any such event
# and before starting the worker services.
#
# Namespaces:
#   news-engine-voices          voice catalog + voice design
#   news-engine-podcast-local   podcast rendering on this box (CosyVoice, ... )
#   news-engine-podcast-modal   podcast rendering on Modal (Qwen3-TTS, ZONOS2)
#
# The script is idempotent: existing namespaces are left untouched.
#
# Usage:  ./scripts/create_namespaces.sh

set -uo pipefail

TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-temporal:7233}"
NAMESPACES=(
  news-engine-voices
  news-engine-podcast-local
  news-engine-podcast-modal
)

if ! docker compose ps --status running temporal | grep -q temporal; then
  echo "ERROR: the 'temporal' service is not running." >&2
  echo "       Start the infrastructure first: docker compose up -d temporal" >&2
  exit 1
fi

existing="$(docker compose exec -T temporal \
  temporal operator namespace list --address "$TEMPORAL_ADDRESS" 2>/dev/null)"

for ns in "${NAMESPACES[@]}"; do
  if grep -qE "(^|[[:space:]])${ns}([[:space:]]|$)" <<<"$existing"; then
    echo "ok       $ns (already exists)"
    continue
  fi
  if docker compose exec -T temporal temporal operator namespace create \
    --namespace "$ns" --address "$TEMPORAL_ADDRESS" >/dev/null 2>&1; then
    echo "created  $ns"
  else
    echo "ERROR: could not create $ns" >&2
    exit 1
  fi
done

echo
docker compose exec -T temporal \
  temporal operator namespace list --address "$TEMPORAL_ADDRESS"
