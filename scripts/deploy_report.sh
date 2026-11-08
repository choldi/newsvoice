#!/usr/bin/env bash
#
# deploy_report.sh — informe de estado del despliegue en Markdown.
#
# Recorre el host y el stack unificado (deploy/docker-compose.yml) y produce
# un Markdown autocontenido con: dependencias del host, configuración (.env,
# secrets enmascarados), servicios desplegados, salud de cada endpoint,
# GPU + runtime externo de qwen3_tts, colecciones/backends, dependencias
# externas (LLM, Telegram) y un índice de hallazgos (❌ crítico / ⚠️ aviso).
#
# El objetivo es pegarlo tal cual en una herramienta AI para que indique
# exactamente qué falta y con qué comandos remediarlo (ver pie del informe).
#
# Uso:
#   ./scripts/deploy_report.sh                 # escribe deploy-report.md
#   ./scripts/deploy_report.sh informe.md      # ruta concreta
#   ./scripts/deploy_report.sh -               # Markdown por stdout (pipe a un AI)
#
# El informe ES el producto: el script sale siempre 0; los problemas se
# listan en la sección "Hallazgos". Todo el progreso va a stderr.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Si el script se ejecuta desde una copia suelta (p. ej. /tmp o `bash -s`),
# la raíz calculada no tendrá el repo: cae al cwd cuando ahí está el árbol.
if [[ ! -d "$REPO_ROOT/deploy" && -d "$PWD/deploy" ]]; then
    REPO_ROOT="$PWD"
fi
cd "$REPO_ROOT" || exit 1

OUT="${1:-deploy-report.md}"
# '-' = Markdown por stdout (para piping directo a una herramienta AI).
# Se traduce a /dev/stdout: un redirect a un fichero literal '-' crearía uno.
[[ "$OUT" == "-" ]] && OUT="/dev/stdout"
COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.yml}"

BODY="$(mktemp)"
trap 'rm -f "$BODY"' EXIT

declare -a MISS=() WARNS=()
OKC=0

# --- Primitivas --------------------------------------------------------------
msg() { printf '· %s\n' "$*" >&2; }
emit() { printf '%s\n' "$*" >>"$BODY"; }

have() { command -v "$1" >/dev/null 2>&1; }

miss() { MISS+=("$*"); }
warn() { WARNS+=("$*"); }
okc() { OKC=$((OKC + 1)); }

sec() {
    emit ""
    emit "## $*"
    emit ""
}

kv() { emit "- **$1**: $2"; }

code_fence() {
    emit '```'
    while IFS= read -r line; do emit "$line"; done
    emit '```'
}

# Escapa pipes y colapsa saltos de línea para usar el valor en una tabla.
esc() {
    printf '%s' "${1-}" | sed -e 's/|/\\|/g' -e 's/[[:space:]]\+/ /g'
}

read_env() {
    local file="$1" key="$2" default="$3" val=""
    [[ -f "$file" ]] || { printf '%s' "$default"; return; }
    val="$(grep -E "^${key}=" "$file" | tail -n1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
    printf '%s' "${val:-$default}"
}

# Claves cuyo valor nunca debe aparecer en claro en el informe.
secret_key() {
    [[ "$1" =~ (TOKEN|SECRET|PASSWORD|PASSWD|_HASH$|_KEY$|^KEY$) ]]
}

fmt_val() {
    local k="$1" v="${2-}"
    if secret_key "$k"; then
        if [[ -z "$v" ]]; then printf '<vacío>'
        elif [[ "$v" == your_* || "$v" == test_bot_token* || "$v" == changeme* ]]; then printf '<placeholder>'
        else printf '<set (%d chars)>' "${#v}"
        fi
        return
    fi
    if [[ -z "$v" ]]; then printf '<vacío>'; return; fi
    if [[ "$v" == your_* ]]; then printf '<placeholder>'; return; fi
    # Enmascara credenciales dentro de URLs (scheme://user:pass@host).
    v="$(printf '%s' "$v" | sed -E 's#(://)[^/@]+@#\1<redact>@#')"
    v="$(printf '%s' "$v" | cut -c1-160)"
    esc "$v"
}

# Cabecera de tabla + filas con todas las claves de un .env (valores maskados).
env_table() {
    local file="$1" line k v first=1
    if [[ ! -f "$file" ]]; then
        emit "> ⚠️ Archivo no encontrado: \`$file\`"
        return 1
    fi
    emit "| Clave | Valor |"
    emit "|---|---|"
    while IFS= read -r line; do
        [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || continue
        k="${line%%=*}"
        v="${line#*=}"
        emit "| \`$k\` | $(fmt_val "$k" "$v") |"
        first=0
    done < "$file"
    [[ "$first" -eq 1 ]] && emit "> sin claves definidas"
    return 0
}

# Subconjunto operativo de claves con estado set/missing (para "qué falta").
curated_table() {
    local file="$1"; shift
    local k v
    emit "| Clave | Estado |"
    emit "|---|---|"
    for k in "$@"; do
        v="$(read_env "$file" "$k" "")"
        if [[ -z "$v" ]]; then
            emit "| \`$k\` | ❌ no definida |"
        else
            emit "| \`$k\` | $(fmt_val "$k" "$v") |"
        fi
    done
}

hit() {
    curl -sf --connect-timeout 3 --max-time 8 "$@" 2>/dev/null
}

valid_json() {
    python3 -c 'import json,sys; json.loads(sys.stdin.read())' <<<"$1" 2>/dev/null
}

# =============================================================================
# Secciones del cuerpo (se escriben en $BODY; los hallazgos van a los arrays)
# =============================================================================

section_host() {
    msg "Host y dependencias"
    local os_line wsl disk mem
    os_line="$(uname -srm 2>/dev/null || echo desconocido)"
    wsl="no"
    grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null && wsl="sí"
    disk="$(df -h "$REPO_ROOT" 2>/dev/null | awk 'NR==2{print $4 " libres de "$2" ("$5" usado)"}')"
    mem="$(free -h 2>/dev/null | awk '/^Mem:/{print $7 " libres de "$2}')"

    sec "1. Host y dependencias"
    kv "Fecha" "$(date -Is 2>/dev/null || date)"
    kv "Sistema" "$os_line (WSL: $wsl)"
    kv "Repositorio" "$REPO_ROOT"
    kv "Disco" "${disk:-n/d}"
    kv "Memoria" "${mem:-n/d}"
    emit ""

    emit "| Dependencia | Estado | Detalle |"
    emit "|---|---|---|"

    local c ver
    for c in git curl python3 uv ffmpeg; do
        if have "$c"; then
            case "$c" in
                python3) ver="$(python3 --version 2>&1)" ;;
                uv) ver="$(uv --version 2>/dev/null)" ;;
                git) ver="$(git --version 2>/dev/null)" ;;
                *) ver="presente" ;;
            esac
            emit "| \`$c\` | ✅ | $(esc "$ver") |"
            okc
        else
            emit "| \`$c\` | ❌ ausente | |"
            case "$c" in
                python3) miss "python3 ausente en el host (necesario para tests, setup y este informe)" ;;
                ffmpeg) warn "ffmpeg ausente en el host: mezcla de audio/transcode de digest lo hace el contenedor, pero faltará para tareas locales" ;;
            esac
        fi
    done

    if have python3.13; then
        emit "| \`python3.13\` | ✅ | $(esc "$(python3.13 --version 2>&1)") |"
        okc
    else
        emit "| \`python3.13\` | ⚠️ ausente | solo hace falta si el venv de qwen3_tts se crea contra 3.13 (overlay GPU) |"
    fi

    if have docker; then
        ver="$(docker --version 2>/dev/null)"
        emit "| \`docker\` | ✅ | $(esc "$ver") |"
        okc
        if docker compose version >/dev/null 2>&1; then
            emit "| \`docker compose\` | ✅ | $(esc "$(docker compose version 2>/dev/null)") |"
            okc
        else
            emit "| \`docker compose\` | ❌ plugin v2 ausente | |"
            miss "Falta el plugin Docker Compose v2 (Deploy.md exige Docker Engine ≥24 + Compose v2)"
        fi
        if docker info 2>/dev/null | grep -qi nvidia; then
            emit "| runtime \`nvidia\` de Docker | ✅ | nvidia-container-toolkit configurado |"
            okc
        else
            emit "| runtime \`nvidia\` de Docker | ❌ no detectado | |"
            warn "Docker no tiene el runtime nvidia: sin \`nvidia-ctk runtime configure\` el gateway arranca sin GPU y qwen3_tts no puede renderizar (Deploy.md §1)"
        fi
    else
        emit "| \`docker\` | ❌ ausente | |"
        miss "Docker ausente: el stack completo es compose-based (Deploy.md §1: Docker Engine ≥24)"
    fi

    if have nvidia-smi; then
        local gpu
        gpu="$(nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>/dev/null | head -2 | paste -sd'; ' -)"
        emit "| \`nvidia-smi\` | ✅ | $(esc "${gpu:-sin GPUs visibles}") |"
        okc
        if [[ -z "$gpu" ]]; then
            warn "nvidia-smi instalado pero sin GPUs visibles"
        fi
    else
        emit "| \`nvidia-smi\` | ❌ ausente/sin driver | |"
        warn "Sin nvidia-smi en el host: no hay GPU accesible (qwen3_tts/omnivoice locales no podrán usar CUDA; revisa driver o modo CPU)"
    fi

    if have tailscale; then
        emit "| \`tailscale\` | ✅ | $(esc "$(tailscale status 2>/dev/null | head -1)") |"
    else
        emit "| \`tailscale\` | – no instalado | solo necesario para Shape B / acceso remoto |"
    fi

    emit ""
    emit "**ssh/keys**: no se ejecutan comprobaciones de conectividad externa más allá de LLM/Telegram (sección 7)."
}

section_config() {
    msg "Configuración (.env)"
    sec "2. Configuración (.env) — valores de secrets enmascarados"

    local f
    for f in deploy/.env apps/news/.env apps/gateway/.env; do
        if [[ -f "$f" ]]; then
            okc
        else
            miss "Falta \`$f\` (copia de su .env.example o ejecuta \`make setup\`)"
        fi
    done

    emit "### \`deploy/.env\` (interpolación de compose)"
    emit ""
    env_table deploy/.env || true
    emit ""

    emit "### \`apps/gateway/.env\` — claves operativas del gateway"
    emit ""
    if [[ -f apps/gateway/.env ]]; then
        curated_table apps/gateway/.env \
            TTS_ENGINE TTS_ENGINES TTS_EXTRA_ENGINES API_KEY \
            QWEN3_TTS_PYTHON QWEN3_TTS_DTYPE QWEN3_TTS_DEVICE \
            TTS_USE_MOCK TTS_FALLBACK_TO_MOCK
        # La key puede venir por compose (deploy/.env GATEWAY_API_KEY → env API_KEY).
        if [[ -z "$(read_env apps/gateway/.env API_KEY '')" && -z "$(read_env deploy/.env GATEWAY_API_KEY '')" ]]; then
            warn "Sin API key del gateway (ni apps/gateway/.env API_KEY ni deploy/.env GATEWAY_API_KEY) — la API queda abierta (hardening P3)"
        fi
        [[ -z "$(read_env apps/gateway/.env TTS_ENGINE '')" ]] &&
            warn "apps/gateway/.env: \`TTS_ENGINE\` sin definir (default omnivoice)"
    else
        emit "> archivo no encontrado"
    fi
    emit ""

    emit "### \`apps/news/.env\` — claves operativas de news-engine"
    emit ""
    if [[ -f apps/news/.env ]]; then
        curated_table apps/news/.env \
            LLM_API_BASE LLM_MODEL TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID \
            TTS_API_BASE_URL TTS_API_KEY NARRATION_ENABLED \
            MODAL_ENABLED MODAL_TOKEN_ID MODAL_TOKEN_SECRET \
            ADMIN_USERNAME ADMIN_API_TOKEN DATABASE_PATH
        local llm tg
        llm="$(read_env apps/news/.env LLM_API_BASE '')"
        [[ -z "$llm" ]] && miss "apps/news/.env: \`LLM_API_BASE\` no definido — ningún workflow LLM puede ejecutarse"
        tg="$(read_env apps/news/.env TELEGRAM_BOT_TOKEN '')"
        if [[ -z "$tg" || "$tg" == your_* ]]; then
            warn "apps/news/.env: \`TELEGRAM_BOT_TOKEN\` sin token real — la entrega Telegram fallará"
        fi
    else
        emit "> archivo no encontrado"
    fi
    emit ""

    emit "### Inventario completo de claves (nombres; valores maskados)"
    emit ""
    emit "| Archivo | Claves definidas |"
    emit "|---|---|"
    local f n
    for f in deploy/.env apps/news/.env apps/gateway/.env; do
        n=0
        if [[ -f "$f" ]]; then
            n="$(grep -cE '^[A-Za-z_][A-Za-z0-9_]*=' "$f" 2>/dev/null || echo 0)"
        fi
        emit "| \`$f\` | $n |"
    done
    emit ""
    emit "El detalle completo de \`apps/gateway/.env\` y \`apps/news/.env\` está en los .env.example del repo; aquí solo se listan las claves operativas para reducir ruido."
}

section_services() {
    msg "Servicios desplegados"
    sec "3. Servicios desplegados (\`$COMPOSE_FILE\`)"

    if ! have docker; then
        emit "> docker no disponible: no se puede listar el stack."
        miss "Stack no evaluable sin docker"
        return
    fi
    if [[ ! -f "$COMPOSE_FILE" ]]; then
        emit "> Archivo compose no encontrado: \`$COMPOSE_FILE\`"
        miss "Falta \`$COMPOSE_FILE\`"
        return
    fi

    local cfg_err cfg_rc
    cfg_err="$(docker compose -f "$COMPOSE_FILE" config 2>&1 >/dev/null)"
    cfg_rc=$?
    if [[ "$cfg_rc" -ne 0 ]]; then
        emit "> ❌ \`docker compose config\` falla (rc=$cfg_rc):"
        emit ""
        emit '```'
        emit "$(printf '%s' "$cfg_err" | tail -8)"
        emit '```'
        miss "docker compose config inválido (revisa $COMPOSE_FILE y deploy/.env): $(printf '%s' "$cfg_err" | tail -1)"
    else
        okc
        if [[ -n "$cfg_err" ]]; then
            emit "> ⚠️ avisos de \`docker compose config\`:"
            emit ""
            emit '```'
            emit "$(printf '%s' "$cfg_err" | tail -6)"
            emit '```'
        fi
    fi

    local ps_rows svc state health status image
    ps_rows="$(docker compose -f "$COMPOSE_FILE" ps -a \
        --format '{{.Service}}|{{.State}}|{{.Health}}|{{.Status}}|{{.Image}}' 2>/dev/null || true)"

    local cfg_services
    cfg_services="$(docker compose -f "$COMPOSE_FILE" config --services 2>/dev/null || true)"

    emit "| Servicio | Estado | Health | Status | Imagen |"
    emit "|---|---|---|---|---|"
    local down=() running=0
    if [[ -n "$ps_rows" ]]; then
        while IFS='|' read -r svc state health status image; do
            [[ -z "$svc" ]] && continue
            local h="${health:--}"
            emit "| \`$svc\` | $state | ${h:-–} | $(esc "$status") | \`${image##*/}\` |"
            if [[ "$state" == "running" ]]; then
                running=$((running + 1)); okc
            else
                down+=("$svc ($state)")
            fi
        done <<<"$ps_rows"
    fi
    # Servicios del config que aún no se han creado.
    local s
    while IFS= read -r s; do
        [[ -z "$s" ]] && continue
        grep -q "^${s}|" <<<"$ps_rows" && continue
        emit "| \`$s\` | – no creado | | | |"
        down+=("$s (no creado)")
    done <<<"$cfg_services"

    # Servicios con profiles (modal/omnivoice): visibles en el compose pero
    # excluidos del config cuando el profile está apagado.
    local prof svc2 profs active_profiles=""
    prof="$(awk '
        /^  [a-z0-9_-]+: *$/ { svc = $1; sub(/:$/, "", svc) }
        /^[[:space:]]*profiles: \[/ {
            match($0, /\[.*\]/)
            print svc "|" substr($0, RSTART + 1, RLENGTH - 2)
        }' "$COMPOSE_FILE" 2>/dev/null || true)"
    while IFS='|' read -r svc2 profs; do
        [[ -z "$svc2" ]] && continue
        if grep -qx "$svc2" <<<"$cfg_services"; then
            emit "| \`$svc2\` | profile activo: \`${profs//\"/}\` | | | |"
        else
            emit "| \`$svc2\` | ⏸ profile inactivo: \`${profs//\"/}\` | | | |"
            active_profiles+=" ${svc2//\"/}"
        fi
    done <<<"$prof"

    emit ""
    kv "Contenedores en marcha" "$running"
    if [[ ${#down[@]} -gt 0 ]]; then
        miss "Servicios sin arrancar: ${down[*]} — ¿stack desplegado? (\`make up\` o \`GPU=1 ./scripts/deploy.sh production\`)"
    fi
    if [[ -n "$active_profiles" ]]; then
        kv "Profiles definidos pero apagados" "$active_profiles (se activan con \`COMPOSE_PROFILES=..\` o los flags de deploy.sh)"
    fi
}

section_health() {
    msg "Salud de endpoints"
    sec "4. Salud de servicios"

    local api_port gw_port api_url gw_url body
    api_port="${API_PORT:-$(read_env deploy/.env API_PORT 8000)}"
    gw_port="${GATEWAY_PORT:-$(read_env deploy/.env GATEWAY_PORT 8001)}"
    api_url="http://localhost:${api_port}/health"
    gw_url="http://localhost:${gw_port}/api/v1/health"

    emit "### news-engine API"
    emit ""
    body="$(hit "$api_url")"
    if [[ -n "$body" ]] && valid_json "$body"; then
        okc
        emit '```json'
        emit "$(printf '%s' "$body" | python3 -m json.tool 2>/dev/null || printf '%s' "$body")"
        emit '```'
    else
        emit "> ❌ \`GET $api_url\` sin respuesta JSON"
        miss "news-engine API no responde en $api_url (contenedor \`api\` caído o stack sin desplegar)"
    fi
    emit ""

    emit "### TTS gateway"
    emit ""
    body="$(hit "$gw_url")"
    if [[ -n "$body" ]] && valid_json "$body"; then
        local gstatus
        gstatus="$(printf '%s' "$body" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))' 2>/dev/null || echo '?')"
        emit '```json'
        emit "$(printf '%s' "$body" | python3 -m json.tool 2>/dev/null || printf '%s' "$body")"
        emit '```'
        emit ""
        local rows
        rows="$(printf '%s' "$body" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for k, v in (d.get("sub_engines") or {}).items():
    print("{}|{}|{}".format(k, v.get("mode", "-"), v.get("model_loaded")))
' 2>/dev/null || true)"
        if [[ -z "$rows" ]]; then
            emit "> sin \`sub_engines\` en la respuesta (versión del gateway sin desglose por motor)."
        else
            emit "| Sub-engine | mode | model_loaded |"
            emit "|---|---|---|"
            local r name mode loaded
            while IFS='|' read -r name mode loaded; do
                [[ -z "$name" ]] && continue
                emit "| \`$name\` | $mode | $loaded |"
                if [[ "$mode" == "NOT_AVAILABLE" || "$mode" == "MOCK" ]]; then
                    warn "gateway: sub-engine \`$name\` en modo $mode (no renderiza)"
                fi
            done <<<"$rows"
        fi
        if [[ "$gstatus" == "ok" ]]; then
            okc
        elif [[ "$gstatus" == "degraded" ]]; then
            warn "gateway \`degraded\` — algún sub-engine no disponible (ver tabla)"
        else
            warn "gateway status='$gstatus'"
        fi
    else
        emit "> ❌ \`GET $gw_url\` sin respuesta JSON"
        miss "TTS gateway no responde en $gw_url (contenedor \`gateway\` caído o arrancado sin el overlay)"
    fi
    emit ""

    emit "### Disponibilidad de engines (\`GET /api/v1/engines\`)"
    emit ""
    local gw_key=""
    gw_key="$(read_env deploy/.env GATEWAY_API_KEY "")"
    [[ -z "$gw_key" ]] && gw_key="$(read_env apps/gateway/.env API_KEY "")"
    local -a gw_hdr=()
    [[ -n "$gw_key" ]] && gw_hdr=(-H "X-API-Key: $gw_key")
    # Sin -f: un 401 debe devolver su body para poder diagnosticarlo.
    body="$(curl -s --connect-timeout 3 --max-time 8 "${gw_hdr[@]}" \
        "http://localhost:${gw_port}/api/v1/engines" 2>/dev/null || true)"
    if [[ -n "$body" ]] && valid_json "$body" && grep -q '"engines"' <<<"$body"; then
        local erows
        erows="$(printf '%s' "$body" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("active|" + str(d.get("active")) + "|")
for e in d.get("engines", []):
    print("{}|{}|{}".format(e.get("name"), e.get("available"), e.get("error") or ""))
' 2>/dev/null || true)"
        local en avail err active_engine=""
        while IFS='|' read -r en avail err; do
            [[ "$en" == "active" ]] && active_engine="$avail"
        done <<<"$erows"
        kv "Engine activo" "${active_engine:-?}"
        emit ""
        emit "| Engine | available | error |"
        emit "|---|---|---|"
        while IFS='|' read -r en avail err; do
            [[ -z "$en" || "$en" == "active" ]] && continue
            emit "| \`$en\` | $avail | $(esc "$err") |"
        done <<<"$erows"
        okc
    elif grep -qi 'authentication required' <<<"$body"; then
        emit "> ❌ 401: el gateway exige \`X-API-Key\` (hardening P3) y no se ha podido autenticar en este informe."
        warn "GET /engines devuelve 401 — configura GATEWAY_API_KEY en deploy/.env (o API_KEY en apps/gateway/.env) para que el informe pueda leerlo"
    else
        emit "> endpoint no accesible (gateway caído o respuesta no-JSON)"
        warn "GET /engines no accesible"
    fi
    emit ""

    emit "### Temporal"
    emit ""
    # La UI actual (temporalio/ui) sirve el SPA con 200+HTML en rutas
    # desconocidas: la API real vive bajo /api/v1 (la vieja era /api).
    local ns sched
    ns="$(curl -s --connect-timeout 3 --max-time 8 -H 'Accept: application/json' \
        "http://localhost:8081/api/v1/namespaces" 2>/dev/null || true)"
    if [[ -n "$ns" ]] && valid_json "$ns"; then
        okc
        emit "- API \`/api/v1/namespaces\` accesible"
        sched="$(curl -s --connect-timeout 3 --max-time 8 -H 'Accept: application/json' \
            "http://localhost:8081/api/v1/namespaces/default/schedules" 2>/dev/null || true)"
        if [[ -n "$sched" ]] && valid_json "$sched"; then
            emit "- Schedules registrados:"
            emit ""
            emit '```'
            emit "$(printf '%s' "$sched" | python3 -c '
import json, sys
d = json.load(sys.stdin)
items = d if isinstance(d, list) else (d.get("schedules") or d.get("items") or [])
for s in items:
    sid = s.get("scheduleId") or s.get("id") or ""
    if sid:
        print(sid)
' 2>/dev/null || printf '(respuesta sin schedules parseables)')"
            emit '```'
            if ! printf '%s' "$sched" | grep -q "daily-digest"; then
                warn "Sin schedules 'daily-digest-*' en Temporal (ejecuta: make schedule)"
            else
                okc
            fi
        else
            warn "No se pudo leer /api/v1/.../schedules desde la UI de Temporal"
        fi
    else
        emit "> ❌ API de Temporal no responde en \`:8081/api/v1/namespaces\` (¿HTML del SPA o contenedor caído?)"
        warn "UI de Temporal inaccesible o API v1 no disponible (¿contenedor temporal-ui caído?)"
    fi
    emit ""

    emit "### Lease del motor GPU (design window)"
    emit ""
    local lease
    lease="$(docker compose -f "$COMPOSE_FILE" exec -T api python -m src.workers.design_lease status 2>/dev/null || true)"
    if [[ -n "$lease" ]]; then
        emit '```json'
        emit "$(printf '%s' "$lease" | head -40)"
        emit '```'
        okc
    else
        emit "> no disponible (¿contenedor \`api\` caído?)"
    fi
    emit ""

    emit "### Temporal workers"
    emit ""
    local w sigs state
    for w in worker narration-worker; do
        state="$(docker compose -f "$COMPOSE_FILE" ps --format '{{.State}}' "$w" 2>/dev/null | tr -d '[:space:]')"
        # 'worker_running' se loguea UNA vez al arrancar: hay que mirar los
        # logs completos, no un tail reciente (un worker ocupado lo pierde).
        sigs="$(docker compose -f "$COMPOSE_FILE" logs "$w" 2>/dev/null | grep -c 'worker_running' || true)"
        if [[ "$state" == "running" && "${sigs:-0}" -ge 1 ]]; then
            emit "- \`$w\`: ✅ en marcha con \`worker_running\` (${sigs}x en logs)"
            okc
        elif [[ "$state" == "running" ]]; then
            emit "- \`$w\`: ⚠️ en marcha pero sin \`worker_running\` en ningún log"
            warn "Worker '$w' corre pero no ha emitido 'worker_running' en sus logs"
        else
            emit "- \`$w\`: ❌ estado '${state:-no creado}'"
            warn "Worker '$w' no está en marcha (estado: ${state:-no creado})"
        fi
    done
    emit ""

    emit "### Base de datos (SQLite)"
    emit ""
    local db_path db_report
    db_path="$(read_env apps/news/.env DATABASE_PATH data/news.db)"
    db_report="$(docker compose -f "$COMPOSE_FILE" exec -T api python -c "
import sqlite3, sys
from pathlib import Path
p = Path('${db_path}')
if not p.exists():
    print('MISSING'); sys.exit(0)
con = sqlite3.connect(p); cur = con.cursor()
def q(sql):
    try:
        row = cur.execute(sql).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None
print('articles:', q('SELECT COUNT(*) FROM articles'))
print('clusters:', q('SELECT COUNT(*) FROM clusters'))
print('deliveries:', q('SELECT COUNT(*) FROM deliveries'))
print('latest_fetched:', q('SELECT MAX(fetched_at) FROM articles'))
print('latest_delivered:', q('SELECT MAX(delivered_at) FROM deliveries'))
" 2>&1 || true)"
    if grep -qiE 'is not running|no such container|error response' <<<"$db_report"; then
        emit "> contenedor \`api\` no disponible: no se puede leer la base de datos."
    elif [[ -n "$db_report" ]]; then
        emit '```'
        emit "$db_report"
        emit '```'
        if grep -q "MISSING" <<<"$db_report"; then
            warn "Base de datos '${db_path}' no encontrada en el contenedor api"
        else
            okc
        fi
    else
        emit "> no legible (¿contenedor \`api\` caído?)"
    fi
}

section_gpu_qwen() {
    msg "GPU y runtime qwen3_tts"
    sec "5. GPU y runtime externo de qwen3_tts"

    # 5.1 venv en el host
    local qdir qpy_env qpy_host
    qdir="$(read_env deploy/.env QWEN3_TTS_DIR "")"
    qpy_env="$(read_env apps/gateway/.env QWEN3_TTS_PYTHON "")"
    if [[ -z "$qpy_env" ]]; then
        qpy_env="<no definida → default del engine>"
        qpy_host="${QWEN3_TTS_PYTHON:-$HOME/qwen3-tts/venv/bin/python}"
    elif [[ "$qpy_env" == /qwen3-tts/* ]]; then
        # Ruta interna al contenedor (mount del overlay): el análogo en el
        # host es QWEN3_TTS_DIR con el mismo sufijo (p. ej. debwk).
        qpy_host="${qdir:-$HOME/qwen3-tts}${qpy_env#/qwen3-tts}"
    else
        qpy_host="$qpy_env"
    fi
    emit "### En el host"
    emit ""
    emit "- \`QWEN3_TTS_PYTHON\` (gateway .env): \`$qpy_env\` → se comprueba en el host como \`$qpy_host\`"
    if [[ -n "$qdir" ]]; then
        if [[ -d "$qdir" ]]; then
            emit "- \`QWEN3_TTS_DIR=$qdir\`: ✅ existe"; okc
        else
            emit "- \`QWEN3_TTS_DIR=$qdir\`: ❌ no existe"
            miss "QWEN3_TTS_DIR ($qdir) no existe en el host: crea el venv de qwen3_tts (Deploy.md §Host runtime)"
        fi
    fi
    if [[ -x "$qpy_host" ]]; then
        emit "- venv host \`$qpy_host\`: ✅ ejecutable"; okc
        local th
        th="$(timeout 60 "$qpy_host" -c 'import torch;print("torch", torch.__version__, "| cuda build:", torch.version.cuda, "| disponible:", torch.cuda.is_available())' 2>&1 | head -3 || true)"
        if [[ -n "$th" ]]; then
            emit ""
            emit '```'
            emit "$th"
            emit '```'
        fi
    else
        emit "- venv host \`$qpy_host\`: ❌ no encontrado"
        if grep -qE '^(TTS_ENGINE|TTS_EXTRA_ENGINES)=.*qwen3' apps/gateway/.env 2>/dev/null; then
            miss "qwen3_tts habilitado en el gateway pero falta el venv host ($qpy_host) — crea el venv e instala torch+qwen-tts (Deploy.md §Host runtime)"
        fi
    fi

    # 5.2 paths del overlay
    local hk paths_ok=1
    for hk in QWEN3_TTS_HOST_PYTHON QWEN3_TTS_HOST_STDLIB QWEN3_TTS_HOST_LIBPYTHON QWEN3_TTS_HOST_INCLUDE QWEN3_TTS_HF_CACHE; do
        local p
        p="$(read_env deploy/.env "$hk" "")"
        [[ -z "$p" ]] && continue
        if [[ -e "$p" ]]; then
            emit "- \`$hk=$p\`: ✅"
        else
            emit "- \`$hk=$p\`: ❌ no existe (docker lo crearía vacío y rompería el runtime)"
            miss "Mount del overlay GPU apunta a un path inexistente: $hk=$p"
            paths_ok=0
        fi
    done

    # 5.3 dentro del contenedor gateway
    emit ""
    emit "### Dentro del contenedor \`gateway\`"
    emit ""
    if ! have docker || ! docker compose -f "$COMPOSE_FILE" ps --status running --format '{{.Service}}' 2>/dev/null | grep -qx gateway; then
        emit "> contenedor \`gateway\` no está en marcha"
    else
        local cpy
        cpy="$(docker compose -f "$COMPOSE_FILE" exec -T gateway printenv QWEN3_TTS_PYTHON 2>/dev/null || true)"
        emit "- \`QWEN3_TTS_PYTHON\` (container): \`${cpy:-<no definida → default ~/qwen3-tts/venv/bin/python>}\`"
        local exists
        exists="$(docker compose -f "$COMPOSE_FILE" exec -T gateway sh -c 'test -x "${QWEN3_TTS_PYTHON:-/qwen3-tts/venv/bin/python}" && echo si || echo no' 2>/dev/null || echo error)"
        if [[ "$exists" == "si" ]]; then
            emit "- intérprete qwen en el contenedor: ✅"; okc
        else
            emit "- intérprete qwen en el contenedor: ❌ no existe (el engine quedará NOT_AVAILABLE)"
            miss "El contenedor gateway no ve el venv de qwen3_tts: despliega con el overlay GPU (GPU=1 ./scripts/deploy.sh production) o corrige QWEN3_TTS_PYTHON/QWEN3_TTS_DIR"
        fi
        local mnt="" gw_cid
        gw_cid="$(docker compose -f "$COMPOSE_FILE" ps -q gateway 2>/dev/null | head -1)"
        if [[ -n "$gw_cid" ]]; then
            mnt="$(docker inspect "$gw_cid" --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}' 2>/dev/null \
                | grep -iE 'qwen|python3|huggingface' || true)"
        fi
        emit "- mounts relevantes:"
        emit ""
        emit '```'
        if [[ -n "$mnt" ]]; then
            while IFS= read -r mline; do emit "$mline"; done <<<"$mnt"
        else
            emit '(sin mounts qwen/python/hf — overlay GPU no aplicado)'
        fi
        emit '```'
        local torch_out
        torch_out="$(docker compose -f "$COMPOSE_FILE" exec -T gateway sh -c '
            P="${QWEN3_TTS_PYTHON:-/qwen3-tts/venv/bin/python}"
            if [ -x "$P" ]; then
                timeout 90 "$P" - <<"PY"
import torch
print("torch:", torch.__version__, "| cuda build:", torch.version.cuda)
print("cuda disponible:", torch.cuda.is_available())
if torch.cuda.is_available():
    print("GPU:", torch.cuda.get_device_name(0), "| cap:", torch.cuda.get_device_capability(0))
print("archs:", torch.cuda.get_arch_list())
PY
            else
                echo "sin intérprete qwen"
            fi
        ' 2>&1 | head -12 || true)"
        if [[ -n "$torch_out" ]]; then
            emit ""
            emit '```'
            emit "$torch_out"
            emit '```'
            if grep -q "cuda disponible: True" <<<"$torch_out"; then
                okc
                if grep -q "archs: \[\]" <<<"$torch_out"; then
                    warn "torch no informa de arquitecturas GPU (¿build CPU o sin GPU visible?)"
                elif ! grep -q "sm_61" <<<"$torch_out" && nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | grep -qi "1070\|1060\|1080\|pascal"; then
                    warn "torch sin sm_61: GPU Pascal detectada pero el wheel de torch ya no la soporta — ancla un build CUDA ≤12.8 (torch==2.6.0+cu124) y re-verifica"
                fi
            fi
        fi
    fi
}

section_collections() {
    msg "Colecciones y backends TTS"
    sec "6. Colecciones y backends TTS (\`apps/news/collections.toml\`)"

    if [[ ! -f apps/news/collections.toml ]]; then
        emit "> archivo no encontrado"
        miss "Falta apps/news/collections.toml"
        return
    fi

    local rows
    rows="$(python3 - <<'PY' 2>&1 || true
import tomllib
with open("apps/news/collections.toml", "rb") as fh:
    data = tomllib.load(fh)
for c in data.get("collections", []):
    spec = c.get("narration_spec") or {}
    print("|".join([
        str(c.get("name")),
        str(c.get("enabled")),
        str(c.get("narration_enabled")),
        str(c.get("time", "-")),
        str(spec.get("tts_backend", "-")),
        str(spec.get("run_in_modal", "-")),
        str(spec.get("voice_source", "-")),
    ]))
PY
)"
    if [[ -z "$rows" ]]; then
        emit "> no se pudo analizar collections.toml"
        warn "collections.toml ilegible para el informe"
        return
    fi

    emit "| Colección | enabled | narration | hora | tts_backend | run_in_modal | voice_source |"
    emit "|---|---|---|---|---|---|---|"
    local cline name enab narr time backend modal vsrc need_omni=0 need_modal_worker=0 need_modal=0
    while IFS='|' read -r name enab narr time backend modal vsrc; do
        [[ -z "$name" ]] && continue
        emit "| \`$name\` | $enab | $narr | $time | \`$backend\` | $modal | $vsrc |"
        if [[ "$backend" == "omni_voice" ]]; then need_omni=1; fi
        if [[ "$backend" == "qwen3_tts" ]]; then
            need_modal_worker=1
            [[ "$modal" == "True" ]] && need_modal=1
        fi
        okc
    done <<<"$rows"

    emit ""
    local modal_enabled
    modal_enabled="$(read_env apps/news/.env MODAL_ENABLED false)"
    local sidecar_state
    sidecar_state="$(docker compose -f "$COMPOSE_FILE" ps --format '{{.State}}' gateway-omnivoice 2>/dev/null | tr -d '[:space:]')"
    local modal_worker_state
    modal_worker_state="$(docker compose -f "$COMPOSE_FILE" ps --format '{{.State}}' narration-modal-worker 2>/dev/null | tr -d '[:space:]')"

    if [[ "$need_omni" -eq 1 ]]; then
        if [[ "$sidecar_state" == "running" ]]; then
            emit "- \`omni_voice\`: sidecar \`gateway-omnivoice\` ✅ en marcha"; okc
        else
            emit "- \`omni_voice\`: sidecar \`gateway-omnivoice\` ❌ no está en marcha"
            miss "Hay colecciones con tts_backend='omni_voice' pero el sidecar OmniVoice no corre (activa el profile 'omnivoice' en compose) — esas narraciones fallarán"
        fi
    fi
    if [[ "$need_modal_worker" -eq 1 && "$modal_worker_state" != "running" ]]; then
        warn "Hay colecciones con tts_backend='qwen3_tts' pero 'narration-modal-worker' (profile modal) no corre — Deploy.md exige ese worker aunque la síntesis sea local (deploy con MODAL=1)"
    fi
    if [[ "$need_modal" -eq 1 && "$modal_enabled" != "true" ]]; then
        warn "Colección con run_in_modal=true pero MODAL_ENABLED=false: se renderizará en el gateway local, no en Modal"
    fi
    emit ""
    kv "TTS_DEFAULT_BACKEND (news)" "$(read_env apps/news/.env TTS_DEFAULT_BACKEND '<no definida → edge>')"
    kv "MODAL_ENABLED" "$modal_enabled"
}

section_external() {
    msg "Dependencias externas"
    sec "7. Dependencias externas"

    local llm_api_base models_url body
    llm_api_base="$(read_env apps/news/.env LLM_API_BASE "")"
    emit "### LLM"
    emit ""
    if [[ -z "$llm_api_base" ]]; then
        emit "- \`LLM_API_BASE\` no definido"
    else
        case "${llm_api_base%/}" in
            */v1) models_url="${llm_api_base%/}/models" ;;
            *) models_url="${llm_api_base%/}/v1/models" ;;
        esac
        body="$(hit "$models_url")"
        if [[ -n "$body" ]] && valid_json "$body"; then
            emit "- \`GET $models_url\`: ✅"
            emit "- modelo configurado: \`$(read_env apps/news/.env LLM_MODEL '<sin LLM_MODEL>')\`"
            emit "- modelos servidos: \`$(printf '%s' "$body" | python3 -c '
import json, sys
d = json.load(sys.stdin)
ids = [m.get("id") for m in d.get("data", []) if m.get("id")]
print(", ".join(ids[:8]) + (" …" if len(ids) > 8 else ""))
' 2>/dev/null || echo n/d)\`"
            okc
        else
            emit "- \`GET $models_url\`: ❌ sin respuesta"
            miss "LLM inaccesible en $models_url — ningún paso de digest/clustering/respelling funcionará (levanta Ollama/llama.cpp o corrige LLM_API_BASE)"
        fi
    fi

    emit ""
    emit "### Telegram"
    emit ""
    local tg_token
    tg_token="$(read_env apps/news/.env TELEGRAM_BOT_TOKEN "")"
    if [[ -z "$tg_token" || "$tg_token" == your_* || "$tg_token" == test_bot_token* ]]; then
        emit "- token: <vacío/placeholder> — comprobación omitida"
    else
        local tg
        tg="$(hit "https://api.telegram.org/bot${tg_token}/getMe")"
        if [[ "$tg" == *'"ok":true'* ]]; then
            emit "- \`getMe\`: ✅ token válido"; okc
        else
            emit "- \`getMe\`: ❌ token rechazado"
            warn "Telegram rechaza el bot token (getMe falló)"
        fi
    fi
    emit "- chat_id: \`$(fmt_val TELEGRAM_CHAT_ID "$(read_env apps/news/.env TELEGRAM_CHAT_ID "")")\`"
}

section_logs() {
    msg "Errores recientes en logs"
    sec "8. Errores recientes (logs, últimas 300 líneas por servicio)"

    local svc lines count sample state
    for svc in api worker narration-worker gateway; do
        state="$(docker compose -f "$COMPOSE_FILE" ps --format '{{.State}}' "$svc" 2>/dev/null | tr -d '[:space:]')"
        if [[ "$state" != "running" ]]; then
            emit "- \`$svc\`: no está en marcha (estado: ${state:-no creado}) — logs omitidos"
            continue
        fi
        lines="$(docker compose -f "$COMPOSE_FILE" logs "$svc" --tail 300 2>/dev/null \
            | grep -iE 'error|exception|traceback' | grep -v 'error_count' | tail -5 || true)"
        count="$(docker compose -f "$COMPOSE_FILE" logs "$svc" --tail 300 2>/dev/null \
            | grep -icE 'error|exception|traceback' || true)"
        if [[ "${count:-0}" -gt 0 ]]; then
            emit ""
            emit "**\`$svc\`** — $count línea(s) con error/exception (últimas 5):"
            emit ""
            emit '```'
            while IFS= read -r sample; do
                [[ -z "$sample" ]] && continue
                printf '%s\n' "$sample" | cut -c1-200 | sed 's/[[:space:]]*$//' >>"$BODY"
            done <<<"$lines"
            emit '```'
            warn "Logs de '$svc' contienen $count línea(s) de error (revisar sección 8)"
        else
            emit "- \`$svc\`: sin errores en las últimas 300 líneas"
            okc
        fi
    done
}

section_repo() {
    msg "Estado del repo"
    sec "9. Estado del repo"
    local branch commit dirty
    branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
    commit="$(git log -1 --format='%h %ad %s' --date=short 2>/dev/null || echo '?')"
    dirty="$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
    kv "Branch" "$branch"
    kv "Último commit" "$commit"
    kv "Cambios sin commitear" "$dirty archivo(s)"
    if [[ "${dirty:-0}" -gt 0 ]]; then
        emit ""
        git status --porcelain 2>/dev/null | head -20 | code_fence
    fi
    emit ""
    kv "COMPOSE_FILE" "$COMPOSE_FILE"
    kv "Puertos" "API=${API_PORT:-$(read_env deploy/.env API_PORT 8000)} · Gateway=${GATEWAY_PORT:-$(read_env deploy/.env GATEWAY_PORT 8001)} · Temporal UI=8081"
}

# =============================================================================
# Ensamblado: cabecera + hallazgos + cuerpo + instrucción para la IA
# =============================================================================

msg "Recogiendo estado del host y del stack…"
section_host
section_config
section_services
section_health
section_gpu_qwen
section_collections
section_external
section_logs
section_repo

HEADER="$(cat <<EOF
# newsvoice — informe de despliegue

> Generado: **$(date -Is 2>/dev/null || date)** · host: **$(hostname 2>/dev/null || echo '?')** · git: **$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')@$(git rev-parse --short HEAD 2>/dev/null || echo '?')**
> Propósito: pegar este Markdown en una herramienta AI para que indique **qué falta** y los comandos de remediación.
> Los valores de secrets aparecen solo como \`<set (N chars)>\` / \`<vacío>\` / \`<placeholder>\`.

## Hallazgos

EOF
)"

{
    printf '%s\n' "$HEADER"
    if [[ ${#MISS[@]} -eq 0 && ${#WARNS[@]} -eq 0 ]]; then
        printf -- '- ✅ Sin problemas críticos ni avisos. %d comprobaciones OK.\n' "$OKC"
    else
        local_any=0
        for m in "${MISS[@]:-}"; do
            [[ -z "$m" ]] && continue
            printf -- '- ❌ **Falta/crítico**: %s\n' "$m"
            local_any=1
        done
        for w in "${WARNS[@]:-}"; do
            [[ -z "$w" ]] && continue
            printf -- '- ⚠️ **Aviso**: %s\n' "$w"
        done
        printf -- '\n> %d comprobaciones OK (detalle en cada sección).\n' "$OKC"
    fi
    printf '\n---\n'
    cat "$BODY"
    cat <<'EOF'

---

## Cómo consumir este informe (para la herramienta AI)

Pega este documento completo con esta instrucción:

> Este es el estado **real** de un despliegue newsvoice. Compáralo con la
> documentación del proyecto (`apps/news/Deploy.md`, `deploy/.env.example`,
> `apps/news/.env.example`, `apps/gateway/.env.example`, `collections.toml`)
> y devuelve: (1) lista priorizada de **qué falta o está roto**, (2) la causa
> raíz de cada hallazgo, (3) los **comandos exactos** para remediarlo y
> (4) qué verificar después. Ignora los valores de secrets (enmascarados).

Notas de contexto:

- `docker compose config` solo muestra los servicios de los profiles activos;
  los servicios con profile apagado aparecen en la tabla como `⏸ profile inactivo`.
- El informe es solo lectura: nada se ejecuta ni se modifica para generarlo.
- Exit code del script: siempre 0 (el informe es el producto).
EOF
} >"${OUT}"

if [[ "$OUT" != "/dev/stdout" ]]; then
    msg "Informe escrito en: $OUT (${#MISS[@]} crítico(s), ${#WARNS[@]} aviso(s), $OKC OK)"
else
    msg "Markdown enviado a stdout (${#MISS[@]} crítico(s), ${#WARNS[@]} aviso(s), $OKC OK)"
fi

exit 0
