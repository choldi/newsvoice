#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

PY="${PYTHON:-python3}"
PLAN_FILE="$(mktemp)"
trap 'rm -f "$PLAN_FILE"' EXIT

engines=()
runtime_args=()
dry_run=0
while (($#)); do
    case "$1" in
        --mode=*|--engine=*|--registry=*|--root=*)
            runtime_args+=("$1")
            ;;
        --mode|--engine|--registry|--root)
            runtime_args+=("$1" "${2:-}")
            shift
            ;;
        --dry-run)
            dry_run=1
            runtime_args+=("$1")
            ;;
        -*)
            echo "unknown option: $1" >&2
            exit 2
            ;;
        *)
            engines+=("$1")
            ;;
    esac
    shift
done

if ((${#engines[@]} == 0)); then
    echo "usage: load.sh ENGINE [ENGINE...] [--mode ENGINE=MODE] [--dry-run]" >&2
    exit 2
fi

engine_args=()
for engine in "${engines[@]}"; do
    engine_args+=(--engine "$engine")
done

"$PY" scripts/engine_runtime.py "${runtime_args[@]}" "${engine_args[@]}" >"$PLAN_FILE"

if ((dry_run)); then
    cat "$PLAN_FILE"
    exit 0
fi

eval "$("$PY" - "$PLAN_FILE" <<'PY'
import json
import shlex
import sys

plan = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"export GPU={1 if plan['gpu'] else 0}")
for env, value in plan["urls"].items():
    print(f"export {env}={shlex.quote(value)}")
for profile in plan["profiles"]:
    key = {
        "modal": "MODAL",
        "qwen3": "QWEN3",
        "omnivoice": "OMNIVOICE",
        "chatterbox": "CHATTERBOX",
    }.get(profile)
    if key:
        print(f"export {key}=1")
PY
)"

./scripts/deploy.sh "${ENVIRONMENT:-production}"

GATEWAY_PORT="${GATEWAY_PORT:-8001}"
if ! curl -sf "http://localhost:${GATEWAY_PORT}/api/v1/engines" >/dev/null 2>&1; then
    echo "gateway engine catalog check failed" >&2
    exit 1
fi

cat "$PLAN_FILE"
