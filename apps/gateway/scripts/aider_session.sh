#!/bin/bash
# scripts/aider_session.sh
#
# Uso:
#   ./scripts/aider_session.sh [argumentos para aider]
#
# Variables útiles:
#   AIDER_MODEL="openai/free-stack-1" ./scripts/aider_session.sh
#   AIDER_ENABLE_WEAK_MODEL=0 ./scripts/aider_session.sh
#   AIDER_ENABLE_EDITOR_MODEL=0 ./scripts/aider_session.sh
#   AIDER_WARMUP_PATTERN="local" ./scripts/aider_session.sh
#   AIDER_WARMUP_TIMEOUT=180 ./scripts/aider_session.sh
#   AIDER_WARMUP_EXTRA_MODELS="openai/local-phi openai/local-llama" ./scripts/aider_session.sh

set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════
# Fase 0: Configuración, rutas, logging y helpers
# ═══════════════════════════════════════════════════════════════════════════

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

MODEL_FILE="${AIDER_MODEL_FILE:-.aider_model_selection.yml}"
DEFAULT_MODEL="${AIDER_DEFAULT_MODEL:-openai/local-qwen}"

# Warm-up
AIDER_WARMUP_TIMEOUT="${AIDER_WARMUP_TIMEOUT:-180}"
AIDER_WARMUP_PATTERN="${AIDER_WARMUP_PATTERN:-local}"
AIDER_WARMUP_EXTRA_MODELS="${AIDER_WARMUP_EXTRA_MODELS:-}"
AIDER_WARMUP_USE_FULL_MODEL_ID="${AIDER_WARMUP_USE_FULL_MODEL_ID:-0}"

# Modelos auxiliares
AIDER_ENABLE_WEAK_MODEL="${AIDER_ENABLE_WEAK_MODEL:-1}"
AIDER_ENABLE_EDITOR_MODEL="${AIDER_ENABLE_EDITOR_MODEL:-1}"

log_info()    { echo -e "${YELLOW}[INFO] $*${NC}"; }
log_success() { echo -e "${GREEN}[OK] $*${NC}"; }
log_warn()    { echo -e "${YELLOW}[WARN] $*${NC}"; }
log_error()   { echo -e "${RED}[ERROR] $*${NC}"; }

phase() {
    echo -e "\n${CYAN}── Fase $1: $2 ─────────────────────────────────────────────${NC}"
}

is_true() {
    local value="${1:-}"
    value="${value,,}"
    [[ "$value" == "1" || "$value" == "true" || "$value" == "yes" || "$value" == "on" ]]
}

get_model_field() {
    local model_id="$1"
    local field="$2"

    python3 - "$MODEL_FILE" "$model_id" "$field" <<'PY' || true
import sys
import yaml

try:
    with open(sys.argv[1], encoding="utf-8") as f:
        data = yaml.safe_load(f)
except Exception:
    sys.exit(0)

models = data.get("models", []) if isinstance(data, dict) else []

for m in models:
    if isinstance(m, dict) and m.get("id") == sys.argv[2]:
        value = m.get(sys.argv[3], "")
        if value is None:
            value = ""
        print(value)
        break
PY
}

get_model_env_var() {
    local model_id="$1"
    local env_var="$2"

    python3 - "$MODEL_FILE" "$model_id" "$env_var" <<'PY' || true
import os
import sys
import yaml

try:
    with open(sys.argv[1], encoding="utf-8") as f:
        data = yaml.safe_load(f)
except Exception:
    sys.exit(0)

models = data.get("models", []) if isinstance(data, dict) else []

for m in models:
    if isinstance(m, dict) and m.get("id") == sys.argv[2]:
        env = m.get("env") or {}
        value = env.get(sys.argv[3], "")
        if value is None:
            value = ""
        print(os.path.expandvars(str(value)))
        break
PY
}

get_aux_model() {
    local aux_type="$1"

    python3 - "$MODEL_FILE" "$aux_type" <<'PY' || true
import sys
import yaml

try:
    with open(sys.argv[1], encoding="utf-8") as f:
        data = yaml.safe_load(f)
except Exception:
    sys.exit(0)

models = data.get("models", []) if isinstance(data, dict) else []

for m in models:
    if isinstance(m, dict) and m.get("type") == sys.argv[2]:
        model_id = m.get("id", "")
        if model_id:
            print(model_id)
            break
PY
}

should_warm_model() {
    local model_id="$1"
    local pattern="${AIDER_WARMUP_PATTERN:-}"

    if [[ -z "$model_id" || -z "$pattern" ]]; then
        return 1
    fi

    local model_name
    model_name="$(get_model_field "$model_id" "name")"

    if printf '%s\n%s\n' "$model_id" "$model_name" | grep -Eqi -- "$pattern"; then
        return 0
    fi

    return 1
}

send_warm_request() {
    local url="$1"
    local api_key="$2"
    local request_model="$3"

    local payload
    payload="$(printf '{"model":"%s","messages":[{"role":"user","content":"ping"}],"max_tokens":1,"stream":false}' "$request_model")"

    local curl_args=(
        -s
        --max-time "$AIDER_WARMUP_TIMEOUT"
        -X POST "$url"
        -H "Content-Type: application/json"
        -d "$payload"
    )

    if [[ -n "$api_key" ]]; then
        curl_args+=(-H "Authorization: Bearer ${api_key}")
    fi

    "${curl_args[@]}" >/dev/null 2>&1
}

warm_model() {
    local model_id="$1"
    local role="$2"

    if [[ -z "$model_id" ]]; then
        return 0
    fi

    if [[ -n "${WARMED_MODELS[$model_id]:-}" ]]; then
        return 0
    fi

    if ! should_warm_model "$model_id"; then
        return 0
    fi

    WARMED_MODELS["$model_id"]=1

    local base_url
    local api_key
    local request_model
    local url

    base_url="$(get_model_env_var "$model_id" "OPENAI_API_BASE")"

    if [[ -z "$base_url" ]]; then
        base_url="${OPENAI_API_BASE:-}"
    fi

    api_key="$(get_model_env_var "$model_id" "OPENAI_API_KEY")"

    if [[ -z "$api_key" ]]; then
        api_key="${OPENAI_API_KEY:-}"
    fi

    if [[ -z "$base_url" ]]; then
        log_warn "Warm-up omitido para ${model_id}: no hay OPENAI_API_BASE definido."
        return 0
    fi

    base_url="${base_url%/}"
    url="${base_url}/chat/completions"

    request_model="$model_id"

    # Normalmente, cuando Aider usa openai/<modelo>, el backend OpenAI-compatible
    # espera recibir <modelo>, no el prefijo openai/.
    if ! is_true "$AIDER_WARMUP_USE_FULL_MODEL_ID" && [[ "$request_model" == openai/* ]]; then
        request_model="${request_model#openai/}"
    fi

    log_info "Warm-up (${role}) ${model_id} -> ${url} (model=${request_model}, timeout=${AIDER_WARMUP_TIMEOUT}s)"

    if send_warm_request "$url" "$api_key" "$request_model"; then
        log_success "Warm-up OK: ${model_id}"
    else
        log_warn "Warm-up falló para ${model_id}. Se continúa igualmente."
    fi
}

declare -A WARMED_MODELS=()

# ═══════════════════════════════════════════════════════════════════════════
# Fase 1: Cargar entorno (.env)
# ═══════════════════════════════════════════════════════════════════════════

phase 1 "Carga de entorno"

if [[ -f .env ]]; then
    log_info "Cargando variables de entorno desde .env..."
    set -a
    source .env
    set +a
    log_success ".env cargado correctamente."
else
    log_warn "No se encontró .env. Las API keys deben estar en el entorno."
fi

# ═══════════════════════════════════════════════════════════════════════════
# Fase 2: Generar contexto fresco
# ═══════════════════════════════════════════════════════════════════════════

phase 2 "Contexto fresco"

if [[ -f scripts/pre_prompt.sh ]]; then
    log_info "Generando contexto fresco..."
    if bash scripts/pre_prompt.sh; then
        log_success "Contexto fresco generado correctamente."
    else
        log_warn "scripts/pre_prompt.sh terminó con error. Se continúa igualmente."
    fi
else
    log_warn "scripts/pre_prompt.sh no encontrado. Continuando sin contexto fresco."
fi

# ═══════════════════════════════════════════════════════════════════════════
# Fase 3: Seleccionar modelo principal y exportar su entorno
# ═══════════════════════════════════════════════════════════════════════════

phase 3 "Selección de modelo principal"

MODEL_FILE_OK=1

if [[ ! -f "$MODEL_FILE" ]]; then
    MODEL_FILE_OK=0
    log_warn "$MODEL_FILE no encontrado. Usando modelo por defecto."
fi

if [[ "$MODEL_FILE_OK" == "1" ]]; then
    log_info "Modelos disponibles:"

    if ! python3 - "$MODEL_FILE" <<'PY'
import sys
import yaml

try:
    with open(sys.argv[1], encoding="utf-8") as f:
        data = yaml.safe_load(f)
except Exception as e:
    print(f"Error leyendo YAML: {e}", file=sys.stderr)
    sys.exit(1)

models = data.get("models", []) if isinstance(data, dict) else []

if not models:
    print("El YAML no contiene modelos.", file=sys.stderr)
    sys.exit(1)

for i, m in enumerate(models, 1):
    icon = str(m.get("icon", "")).strip()
    name = m.get("name", m.get("id", "unknown"))
    typ = m.get("type", "?")

    if icon:
        print(f"  {i:2}. {icon} {name} ({typ})")
    else:
        print(f"  {i:2}. {name} ({typ})")
PY
    then
        MODEL_FILE_OK=0
        log_warn "No se pudo leer $MODEL_FILE. Usando modelo por defecto."
    fi
fi

if [[ "$MODEL_FILE_OK" == "1" ]]; then
    if [[ -n "${AIDER_MODEL:-}" ]]; then
        CHOICE="$AIDER_MODEL"
        log_info "Modelo forzado por AIDER_MODEL: ${CHOICE}"
    elif [[ -t 0 ]]; then
        echo ""
        read -r -p "Selecciona un modelo (número, ID o Enter para el primero): " CHOICE
        CHOICE="${CHOICE:-1}"
    else
        CHOICE=1
        log_info "Entrada no interactiva. Usando el primer modelo."
    fi

    SELECTED_MODEL="$(
        python3 - "$MODEL_FILE" "$CHOICE" "$DEFAULT_MODEL" <<'PY' || true
import sys
import yaml

model_file, choice, default_model = sys.argv[1], sys.argv[2], sys.argv[3]

try:
    with open(model_file, encoding="utf-8") as f:
        data = yaml.safe_load(f)
except Exception:
    print(default_model)
    sys.exit(0)

models = data.get("models", []) if isinstance(data, dict) else []
choice = (choice or "").strip()

if not choice:
    print(default_model)
    sys.exit(0)

if choice.isdigit():
    idx = int(choice) - 1
    if 0 <= idx < len(models):
        print(models[idx].get("id", default_model))
    else:
        print(default_model)
    sys.exit(0)

for m in models:
    if m.get("id") == choice or m.get("name") == choice:
        print(m.get("id", default_model))
        sys.exit(0)

print(default_model)
PY
    )"

    if [[ -z "$SELECTED_MODEL" ]]; then
        log_warn "Selección inválida. Usando modelo por defecto."
        SELECTED_MODEL="$DEFAULT_MODEL"
    fi

    log_success "Modelo principal seleccionado: ${SELECTED_MODEL}"

    ENV_SCRIPT="$(
        python3 - "$MODEL_FILE" "$SELECTED_MODEL" <<'PY' || true
import os
import shlex
import sys
import yaml

model_file, model_id = sys.argv[1], sys.argv[2]

try:
    with open(model_file, encoding="utf-8") as f:
        data = yaml.safe_load(f)
except Exception:
    sys.exit(0)

models = data.get("models", []) if isinstance(data, dict) else []
selected = None

for m in models:
    if isinstance(m, dict) and m.get("id") == model_id:
        selected = m
        break

if selected is None:
    sys.exit(0)

env = selected.get("env") or {}

# Compatibilidad con librerías que miran OPENAI_BASE_URL.
if env.get("OPENAI_API_BASE") and not env.get("OPENAI_BASE_URL"):
    env["OPENAI_BASE_URL"] = env["OPENAI_API_BASE"]

for k, v in env.items():
    if not isinstance(k, str):
        continue
    value = "" if v is None else str(v)
    expanded = os.path.expandvars(value)
    print(f"export {k}={shlex.quote(expanded)}")
PY
    )"

    if [[ -n "$ENV_SCRIPT" ]]; then
        eval "$ENV_SCRIPT"
        log_success "Entorno exportado para ${SELECTED_MODEL}."
    else
        log_warn "El modelo ${SELECTED_MODEL} no definió variables de entorno."
    fi

    # Si el modelo define OPENAI_API_BASE pero no OPENAI_BASE_URL, sincronizamos.
    if [[ -n "${OPENAI_API_BASE:-}" && -z "${OPENAI_BASE_URL:-}" ]]; then
        export OPENAI_BASE_URL="$OPENAI_API_BASE"
        log_info "Sincronizado OPENAI_BASE_URL con OPENAI_API_BASE."
    fi
else
    SELECTED_MODEL="$DEFAULT_MODEL"
    log_warn "Usando modelo por defecto: ${SELECTED_MODEL}"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Fase 4: Resolver modelos auxiliares (weak/editor)
# ═══════════════════════════════════════════════════════════════════════════

phase 4 "Modelos auxiliares"

WEAK_MODEL=""
EDITOR_MODEL=""

if is_true "$AIDER_ENABLE_WEAK_MODEL"; then
    WEAK_MODEL="$(get_aux_model "weak")"
    if [[ -n "$WEAK_MODEL" ]]; then
        log_info "Weak model detectado: ${WEAK_MODEL}"
    else
        log_info "No se encontró weak model."
    fi
else
    log_info "Weak model deshabilitado por AIDER_ENABLE_WEAK_MODEL."
fi

if is_true "$AIDER_ENABLE_EDITOR_MODEL"; then
    EDITOR_MODEL="$(get_aux_model "editor")"
    if [[ -n "$EDITOR_MODEL" ]]; then
        log_info "Editor model detectado: ${EDITOR_MODEL}"
    else
        log_info "No se encontró editor model."
    fi
else
    log_info "Editor model deshabilitado por AIDER_ENABLE_EDITOR_MODEL."
fi

# ═══════════════════════════════════════════════════════════════════════════
# Fase 5: Warm-up de modelos locales
# ═══════════════════════════════════════════════════════════════════════════

phase 5 "Warm-up de modelos locales"

log_info "Patrón de warm-up: ${AIDER_WARMUP_PATTERN:-<vacío>}"
log_info "Timeout de warm-up: ${AIDER_WARMUP_TIMEOUT}s"

warm_model "$SELECTED_MODEL" "principal"
warm_model "$WEAK_MODEL" "weak"
warm_model "$EDITOR_MODEL" "editor"

if [[ -n "$AIDER_WARMUP_EXTRA_MODELS" ]]; then
    read -r -a EXTRA_WARM_MODELS <<< "$AIDER_WARMUP_EXTRA_MODELS"
    for extra_model in "${EXTRA_WARM_MODELS[@]}"; do
        warm_model "$extra_model" "extra"
    done
fi

if [[ "${#WARMED_MODELS[@]}" -eq 0 ]]; then
    log_info "No se calentó ningún modelo."
fi

# ═══════════════════════════════════════════════════════════════════════════
# Fase 6: Construir argumentos de Aider
# ═══════════════════════════════════════════════════════════════════════════

phase 6 "Construcción de argumentos de Aider"

AIDER_ARGS=("--model" "$SELECTED_MODEL")

# Refuerzo para endpoints OpenAI-compatible: evita que variables previas
# como OPENAI_BASE_URL terminen mandando la petición a otro servidor.
if [[ "$SELECTED_MODEL" == openai/* ]]; then
    if [[ -n "${OPENAI_API_BASE:-}" ]]; then
        AIDER_ARGS+=("--openai-api-base" "$OPENAI_API_BASE")
        log_info "Forzando --openai-api-base ${OPENAI_API_BASE} por CLI."
    fi

    if [[ -n "${OPENAI_API_KEY:-}" ]]; then
        AIDER_ARGS+=("--openai-api-key" "$OPENAI_API_KEY")
        log_info "Forzando --openai-api-key por CLI."
    fi
fi

if [[ -n "$WEAK_MODEL" && "$WEAK_MODEL" != "$SELECTED_MODEL" ]]; then
    AIDER_ARGS+=("--weak-model" "$WEAK_MODEL")
fi

if [[ -n "$EDITOR_MODEL" && "$EDITOR_MODEL" != "$SELECTED_MODEL" ]]; then
    AIDER_ARGS+=("--editor-model" "$EDITOR_MODEL")
fi

if [[ -f .aider_fresh_context.md ]]; then
    AIDER_ARGS+=("--read" ".aider_fresh_context.md")
    log_info "Añadido contexto fresco: .aider_fresh_context.md"
fi

# Argumentos pasados al script.
AIDER_ARGS+=("$@")

# Argumentos extra desde variable de entorno.
if [[ -n "${AIDER_EXTRA_ARGS:-}" ]]; then
    read -r -a EXTRA_ARGS <<< "$AIDER_EXTRA_ARGS"
    AIDER_ARGS+=("${EXTRA_ARGS[@]}")
    log_info "Añadidos argumentos extra desde AIDER_EXTRA_ARGS: ${AIDER_EXTRA_ARGS}"
fi

log_info "Argumentos finales: aider ${AIDER_ARGS[*]}"

# ═══════════════════════════════════════════════════════════════════════════
# Fase 7: Ejecutar Aider
# ═══════════════════════════════════════════════════════════════════════════

phase 7 "Ejecución de Aider"

log_info "Iniciando sesión con modelo ${SELECTED_MODEL}..."

set +e
aider "${AIDER_ARGS[@]}"
exit_code=$?
set -e

log_info "Aider terminó con código ${exit_code}."

# ═══════════════════════════════════════════════════════════════════════════
# Fase 8: Actualizar memoria del proyecto
# ═══════════════════════════════════════════════════════════════════════════

phase 8 "Actualización de memoria"

TEMPLATE="docs/AIDER_MEMORY_TEMPLATE.md"
OUTPUT="docs/AIDER_MEMORY.md"
PHASE_FILE=".aider_phase"

if [[ -f "$TEMPLATE" ]]; then
    log_info "Actualizando memoria en $OUTPUT..."

    DATE="$(date '+%d de %B de %Y %H:%M')"
    BRANCH="$(git branch --show-current 2>/dev/null || echo "unknown")"
    RECENT_COMMITS="$(git log --oneline -5 2>/dev/null || echo "No git history")"
    MODIFIED_FILES="$(git diff --name-only HEAD~5 2>/dev/null | sort -u | sed 's/^/  - /' || echo "  - (no git diff)")"
    TODOS="$(grep -rn "TODO\|FIXME\|HACK" src/ --include="*.py" --include="*.js" --include="*.ts" --include="*.java" --include="*.go" --include="*.rs" 2>/dev/null | head -15 | sed 's/^/  - /' || echo "  - (none)")"
    PHASE="$(cat "$PHASE_FILE" 2>/dev/null || echo "1 (Inicial)")"

    STATE_FILE="docs/AIDER_STATE.md"
    STATE="$(cat "$STATE_FILE" 2>/dev/null || echo "Desarrollo activo de la Fase ${PHASE}.")"

    export AIDER_DATE="$DATE"
    export AIDER_PHASE="$PHASE"
    export AIDER_BRANCH="$BRANCH"
    export AIDER_STATE="$STATE"
    export AIDER_RECENT_COMMITS="$RECENT_COMMITS"
    export AIDER_MODIFIED_FILES="$MODIFIED_FILES"
    export AIDER_TODOS="$TODOS"

    if python3 <<'PYEOF'
import os
from pathlib import Path

template = Path("docs/AIDER_MEMORY_TEMPLATE.md").read_text(encoding="utf-8")
output_file = Path("docs/AIDER_MEMORY.md")

replaces = {
    "{{DATE}}": os.environ.get("AIDER_DATE", ""),
    "{{PHASE}}": os.environ.get("AIDER_PHASE", ""),
    "{{BRANCH}}": os.environ.get("AIDER_BRANCH", ""),
    "{{STATE}}": os.environ.get("AIDER_STATE", ""),
    "{{RECENT_COMMITS}}": os.environ.get("AIDER_RECENT_COMMITS", ""),
    "{{MODIFIED_FILES}}": os.environ.get("AIDER_MODIFIED_FILES", ""),
    "{{TODOS}}": os.environ.get("AIDER_TODOS", ""),
}

for old, new in replaces.items():
    template = template.replace(old, new)

output_file.write_text(template, encoding="utf-8")
print("Memoria actualizada correctamente.")
PYEOF
    then
        log_success "Memoria actualizada en $OUTPUT"
    else
        log_warn "No se pudo actualizar la memoria."
    fi
else
    log_warn "Plantilla $TEMPLATE no encontrada. Saltando actualización de memoria."
fi

# ═══════════════════════════════════════════════════════════════════════════
# Fase 9: Fin de sesión
# ═══════════════════════════════════════════════════════════════════════════

phase 9 "Fin de sesión"

if [[ "$exit_code" -eq 0 ]]; then
    log_success "Sesión finalizada correctamente."
else
    log_error "Sesión finalizada con código ${exit_code}."
fi

exit "$exit_code"

