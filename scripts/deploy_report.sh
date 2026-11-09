#!/usr/bin/env bash
#
# deploy_report.sh — informe de estado del despliegue en Markdown.
#
# Recorre el host y el stack unificado (deploy/docker-compose.yml) y produce
# un Markdown autocontenido con: dependencias del host, configuración (.env,
# secrets enmascarados), servicios desplegados, salud de cada endpoint,
# GPU + sidecars de engines pesados (gateway-qwen3 / gateway-omnivoice),
# colecciones/backends, dependencias externas (LLM, Telegram), un índice de
# hallazgos (❌ crítico / ⚠️ aviso) y un análisis en profundidad que empareja
# cada problema con su causa raíz y su solución (comandos incluidos).
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
ANALYSIS="$(mktemp)"
trap 'rm -f "$BODY" "$ANALYSIS"' EXIT

declare -a MISS=() WARNS=()
# Análisis en profundidad: cada problema con su causa y su solución.
declare -a D_SEV=() D_TITLE=() D_WHAT=() D_CAUSE=() D_IMPACT=() D_FIX=() D_CMD=()
OKC=0

# --- Primitivas --------------------------------------------------------------
msg() { printf '· %s\n' "$*" >&2; }
emit() { printf '%s\n' "$*" >>"$BODY"; }
emit_a() { printf '%s\n' "$*" >>"$ANALYSIS"; }

have() { command -v "$1" >/dev/null 2>&1; }

miss() { MISS+=("$*"); }
warn() { WARNS+=("$*"); }
okc() { OKC=$((OKC + 1)); }

# deep <bad|warn> <título> <qué> <causa> <impacto> <solución> [comando...]
# Registra un problema del análisis; el título también entra en el índice.
deep() {
    local sev="$1" title="$2" what="$3" cause="$4" impact="$5" fix="$6"
    shift 6
    D_SEV+=("$sev")
    D_TITLE+=("$title")
    D_WHAT+=("$what")
    D_CAUSE+=("$cause")
    D_IMPACT+=("$impact")
    D_FIX+=("$fix")
    D_CMD+=("$(printf '%s\n' "$@")")
    # El índice no se duplica: si otra sección ya reportó este título, el
    # análisis aporta la detalle sin repetir la línea en "Hallazgos".
    local seen item
    seen=0
    for item in "${MISS[@]:-}" "${WARNS[@]:-}"; do
        if [[ -n "$item" && "$item" == "$title" ]]; then
            seen=1
            break
        fi
    done
    if [[ "$seen" -eq 1 ]]; then
        return 0
    fi
    if [[ "$sev" == "bad" ]]; then
        miss "$title"
    else
        warn "$title"
    fi
}

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
                python3)
                    deep bad "python3 ausente en el host" \
                        "El binario python3 no está en PATH." \
                        "Host recién aprovisionado sin el intérprete de Python (o solo con otro nombre)." \
                        "No corren el asistente, los tests, el lint ni este informe; las apps en local son imposibles." \
                        "Instalar Python 3.11+ con el gestor de paquetes del sistema." \
                        "sudo apt-get install -y python3 python3-pip" ;;
                uv)
                    deep warn "uv ausente en el host" \
                        "El binario uv no está en PATH (opcional)." \
                        "Nunca se instaló el gestor de entornos de Astral." \
                        "Sin él hay que usar la alternativa pip/venv (make venv): los pasos «uv sync» del asistente no funcionarán." \
                        "Instalar uv (recomendado) o elegir el método «pip · un .venv por app»." \
                        "curl -LsSf https://astral.sh/uv/install.sh | sh" ;;
                ffmpeg)
                    deep warn "ffmpeg ausente en el host" \
                        "El binario ffmpeg no está en PATH." \
                        "Host sin los códecs multimedia (el proyecto no los empaqueta)." \
                        "En Docker el contenedor ya lo lleva, pero las tareas locales de audio (mezcla, transcode, loudnorm) fallarán." \
                        "Instalar ffmpeg del repositorio del sistema." \
                        "sudo apt-get install -y ffmpeg" ;;
                git|curl)
                    deep warn "$c ausente en el host" \
                        "El binario $c no está en PATH." \
                        "Instalación mínima del sistema." \
                        "Sin $c no se puede clonar/actualizar el repo ni descargar dependencias." \
                        "Instalar el paquete del sistema." \
                        "sudo apt-get install -y $c" ;;
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
            deep bad "Docker Compose v2 ausente" \
                "El plugin \`docker compose\` no responde." \
                "Docker instalado sin el paquete docker-compose-plugin (o solo con el binario legacy v1)." \
                "Ningún comando del proyecto funciona: make up, make deploy y scripts/deploy.sh usan la sintaxis del plugin v2." \
                "Instalar el plugin v2." \
                "sudo apt-get install -y docker-compose-plugin"
        fi
        if docker info 2>/dev/null | grep -qi nvidia; then
            emit "| runtime \`nvidia\` de Docker | ✅ | nvidia-container-toolkit configurado |"
            okc
        else
            emit "| runtime \`nvidia\` de Docker | ❌ no detectado | |"
            deep warn "Docker sin runtime nvidia" \
                "\`docker info\` no anuncia el runtime nvidia." \
                "Falta nvidia-container-toolkit o no se ejecutó \`nvidia-ctk runtime configure\`." \
                "Los contenedores no reciben la GPU: qwen3_tts y omnivoice locales quedan en CPU (o no arrancan) aunque la tarjeta exista." \
                "Instalar el toolkit, registrar el runtime y reiniciar el demonio." \
                "sudo apt-get install -y nvidia-container-toolkit" \
                "sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
        fi
    else
        emit "| \`docker\` | ❌ ausente | |"
        deep bad "Docker ausente" \
            "El binario docker no está en PATH." \
            "Host recién creado sin el motor de contenedores." \
            "El stack completo es compose-based: no hay forma de levantar nada (Deploy.md §1: Docker Engine ≥24 + Compose v2)." \
            "Instalar Docker con el script oficial y dar permisos al usuario." \
            "curl -fsSL https://get.docker.com | sh" \
            "sudo usermod -aG docker \$USER" \
            "newgrp docker   # o cierra y vuelve a entrar"
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
        deep warn "Sin nvidia-smi en el host" \
            "El binario nvidia-smi no está (sin driver NVIDIA o sin tarjeta)." \
            "Host sin el driver NVIDIA instalado (o VM sin GPU passthrough)." \
            "qwen3_tts y omnivoice locales no pueden usar CUDA: solo quedan los engines CPU/cloud y Modal." \
            "Instalar el driver y validar antes de desplegar con GPU=1." \
            "ubuntu-drivers autoinstall && sudo reboot" \
            "nvidia-smi"
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
            QWEN3_TTS_REMOTE_URL OMNIVOICE_REMOTE_URL \
            FISH_API_KEY OPENAI_TTS_BASE_URL \
            TTS_USE_MOCK TTS_FALLBACK_TO_MOCK
        # La key puede venir por compose (deploy/.env GATEWAY_API_KEY → env API_KEY).
        if [[ -z "$(read_env apps/gateway/.env API_KEY '')" && -z "$(read_env deploy/.env GATEWAY_API_KEY '')" ]]; then
            warn "Gateway sin API key"
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
        [[ -z "$llm" ]] && miss "LLM_API_BASE sin definir"
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
    msg "GPU y sidecars de engines pesados"
    sec "5. GPU y sidecars de engines pesados (\`gateway-qwen3\`, \`gateway-omnivoice\`)"

    emit "> El runtime legacy de qwen3 (venv en el host: \`QWEN3_TTS_DIR\`,"
    emit "> \`QWEN3_TTS_PYTHON\`, montajes de intérprete) quedó retirado: torch y"
    emit "> \`qwen-tts\` viven solo en la imagen del sidecar."
    emit ""

    # --- 5.1 GPU del host ----------------------------------------------------
    emit "### GPU del host"
    emit ""
    if have nvidia-smi; then
        local gpu_line
        gpu_line="$(nvidia-smi --query-gpu=name,memory.total,driver_version \
            --format=csv,noheader 2>/dev/null | head -3 | paste -sd'; ' -)"
        emit "- \`nvidia-smi\`: ${gpu_line:-sin GPUs visibles}"
        if [[ -n "$gpu_line" ]]; then okc; fi
        if have docker && docker info 2>/dev/null | grep -qi nvidia; then
            emit "- runtime \`nvidia\` de Docker: ✅"
            okc
        else
            emit "- runtime \`nvidia\` de Docker: ❌ no detectado"
            warn "Docker sin runtime nvidia: los sidecars GPU no verán la tarjeta (\`sudo nvidia-ctk runtime configure && sudo systemctl restart docker\`)"
        fi
    else
        emit "- \`nvidia-smi\`: ❌ ausente (sin driver NVIDIA o sin GPU)"
        emit "  (el detalle y la remediación están en la sección 1 y en el análisis)"
    fi
    emit ""

    # --- 5.2 sidecars --------------------------------------------------------
    local running="" qwen_url omni_url qwen_up omni_up
    if have docker; then
        running="$(docker compose -f "$COMPOSE_FILE" ps --status running \
            --format '{{.Service}}' 2>/dev/null || true)"
    fi
    qwen_url="$(read_env deploy/.env QWEN3_TTS_REMOTE_URL "")"
    [[ -z "$qwen_url" ]] && qwen_url="$(read_env apps/gateway/.env QWEN3_TTS_REMOTE_URL "")"
    omni_url="$(read_env apps/gateway/.env OMNIVOICE_REMOTE_URL "")"
    qwen_up="⏸ parado / no creado"
    grep -qx "gateway-qwen3" <<<"$running" && qwen_up="✅ running"
    omni_up="⏸ parado / no creado"
    grep -qx "gateway-omnivoice" <<<"$running" && omni_up="✅ running"

    emit "### Sidecars internos"
    emit ""
    emit "| Sidecar | Perfil compose | URL que ve el gateway | Contenedor |"
    emit "|---|---|---|---|"
    emit "| \`gateway-qwen3\` | \`qwen3\` | \`${qwen_url:-<vacía>}\` | $qwen_up |"
    emit "| \`gateway-omnivoice\` | \`omnivoice\` | \`${omni_url:-<vacía>}\` | $omni_up |"
    emit ""

    local extra route
    extra="$(read_env apps/gateway/.env TTS_EXTRA_ENGINES "")"
    route="$(read_env apps/gateway/.env TTS_ENGINES "")"

    if ! grep -q "qwen3" <<<"$extra$route"; then
        emit "- \`qwen3_tts\` no está en \`TTS_EXTRA_ENGINES\` ni en \`TTS_ENGINES\`: el sidecar no se usa"
        emit ""
    elif [[ -z "$qwen_url" ]]; then
        emit "- ❌ \`qwen3_tts\` está habilitado pero \`QWEN3_TTS_REMOTE_URL\` está vacía:"
        emit "  el engine queda NOT_AVAILABLE y sus voces degradan a Edge."
        emit "- Solución: añadir \`QWEN3_TTS_REMOTE_URL=http://gateway-qwen3:8000/api/v1\`"
        emit "  a \`deploy/.env\` y desplegar con \`GPU=1 QWEN3=1 ./scripts/deploy.sh production\`."
        emit ""
        miss "qwen3_tts habilitado en el gateway sin QWEN3_TTS_REMOTE_URL (engine NOT_AVAILABLE, voces → Edge)"
    elif [[ "$qwen_up" == ✅* ]]; then
        emit "- ✅ sidecar \`gateway-qwen3\` arriba; el gateway le hace proxy (nombre público \`qwen3_tts\`)"
        okc
        emit ""
        emit "#### Pruebas dentro de \`gateway-qwen3\`"
        emit ""
        local q_health q_torch q_vol
        q_health="$(docker compose -f "$COMPOSE_FILE" exec -T gateway-qwen3 python -c \
            'import httpx;print(httpx.get("http://127.0.0.1:8000/api/v1/health/live",timeout=6).status_code)' \
            2>/dev/null || echo "error")"
        if [[ "$q_health" == "200" ]]; then
            emit "- \`/api/v1/health/live\` del sidecar: ✅ 200"
            okc
        else
            emit "- \`/api/v1/health/live\` del sidecar: ❌ ($q_health)"
            miss "gateway-qwen3 responde $q_health al healthcheck: revisa \`docker compose logs gateway-qwen3\`"
        fi
        q_torch="$(docker compose -f "$COMPOSE_FILE" exec -T gateway-qwen3 \
            /opt/qwen3-tts/venv/bin/python -c \
            'import torch;print("torch",torch.__version__,"| cuda:",torch.version.cuda,"| disponible:",torch.cuda.is_available(),"| GPU:",torch.cuda.get_device_name(0) if torch.cuda.is_available() else "n/a")' \
            2>&1 | tail -2 || true)"
        if [[ -n "$q_torch" ]]; then
            emit ""
            emit '```'
            emit "$q_torch"
            emit '```'
        fi
        if ! grep -qi "disponible: True" <<<"$q_torch"; then
            warn "torch en gateway-qwen3 no ve CUDA: el sidecar estático en CPU (¿overlay GPU aplicado?)"
        else
            okc
        fi
        q_vol="$(docker volume ls --format '{{.Name}}' 2>/dev/null | grep -c 'qwen3_hf_cache' || true)"
        if [[ "$q_vol" -gt 0 ]]; then
            emit "- volumen \`qwen3_hf_cache\` (pesos 1.7B): ✅ creado"
            okc
        else
            emit "- volumen \`qwen3_hf_cache\`: ⏸ no creado (se crea en la primera síntesis)"
        fi
    else
        emit "- ❌ \`QWEN3_TTS_REMOTE_URL\` apunta a \`gateway-qwen3\` pero el contenedor no corre:"
        emit "  el proxy fallará y las voces qwen3 degradan a Edge."
        emit "- Solución: \`GPU=1 QWEN3=1 ./scripts/deploy.sh production\`"
        emit "  (o \`docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.gpu.yml --profile qwen3 up -d\`)."
        emit ""
        miss "gateway-qwen3 configurado en QWEN3_TTS_REMOTE_URL pero el contenedor está parado"
    fi

    emit ""
    if [[ "$omni_up" == ✅* && -n "$omni_url" ]]; then
        emit "- ✅ sidecar \`gateway-omnivoice\` arriba (\`$omni_url\`); el engine \`omnivoice_remote\` lo alcanza"
        okc
    elif grep -q "omnivoice_remote" <<<"$extra$route"; then
        if [[ -z "$omni_url" ]]; then
            emit "- ❌ \`omnivoice_remote\` está en \`TTS_EXTRA_ENGINES\` pero \`OMNIVOICE_REMOTE_URL\` está vacía"
            miss "omnivoice_remote habilitado sin OMNIVOICE_REMOTE_URL (engine no disponible)"
        else
            emit "- ❌ \`OMNIVOICE_REMOTE_URL=$omni_url\` pero el contenedor no corre"
            miss "gateway-omnivoice parado con OMNIVOICE_REMOTE_URL configurada (\`docker compose --profile omnivoice up -d gateway-omnivoice\`)"
        fi
    else
        emit "- \`omnivoice_remote\` inactivo (sin URL o fuera de \`TTS_EXTRA_ENGINES\`)"
    fi

    # Reserva de GPU del contenedor gateway (overlay).
    emit ""
    local gw_cid gw_gpu
    gw_cid="$(docker compose -f "$COMPOSE_FILE" ps -q gateway 2>/dev/null | head -1)"
    gw_gpu=""
    if [[ -n "$gw_cid" ]]; then
        gw_gpu="$(docker inspect "$gw_cid" --format '{{.HostConfig.DeviceRequests}}' 2>/dev/null || true)"
    fi
    if [[ -z "$gw_gpu" || "$gw_gpu" == "[]" ]]; then
        emit "- contenedor \`gateway\` sin reserva GPU: falta \`deploy/docker-compose.gpu.yml\`"
        warn "gateway sin reserva GPU: despliega con \`GPU=1\` para añadir el overlay del GPU"
    else
        emit "- contenedor \`gateway\` con reserva GPU: ✅"
        okc
    fi

    # Variables legacy que ya no hacen nada.
    local legacy
    legacy="$(read_env deploy/.env QWEN3_TTS_DIR "")"
    [[ -z "$legacy" ]] && legacy="$(read_env apps/gateway/.env QWEN3_TTS_PYTHON "")"
    if [[ -n "$legacy" ]]; then
        emit ""
        emit "- ⚠️ variable legacy presente (\`$legacy\`): sin efecto desde el sidecar; puedes borrarla"
        warn "Variable legacy de venv qwen3 en el .env ($legacy): el runtime es el sidecar gateway-qwen3; bórrala"
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
            deep bad "LLM inaccesible" \
                "GET $models_url no devuelve JSON válido." \
                "El servidor LLM (Ollama/llama.cpp) está parado, en otro puerto, o la ruta no es OpenAI-compatible en $models_url." \
                "Ningún paso del digest (resumen, clustering, respelling, verificación) puede ejecutarse aunque el resto del stack esté sano." \
                "Arrancar el servidor LLM y alinear LLM_API_BASE con la ruta real de /v1/models." \
                "curl -sS $models_url   # debe responder JSON con \"data\": [...]" \
                "# grep -E '^(LLM_API_BASE|LLM_MODEL)=' apps/news/.env"
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

render_analysis() {
    emit_a "## Análisis en profundidad"
    emit_a ""
    if [[ ${#D_TITLE[@]} -eq 0 ]]; then
        emit_a "> Sin problemas detectados en el análisis en profundidad."
        emit_a ""
        return 0
    fi
    emit_a "Cada entrada expone **qué** falla, **por qué**, **qué implica** y **cómo arreglarlo**."
    emit_a ""
    local idx n=0 mark
    for ((idx = 0; idx < ${#D_TITLE[@]}; idx++)); do
        if [[ "${D_SEV[idx]}" == "bad" ]]; then mark="❌"; else mark="⚠️"; fi
        n=$((n + 1))
        emit_a "### $mark $n. ${D_TITLE[idx]}"
        emit_a ""
        emit_a "- **Qué**: ${D_WHAT[idx]}"
        emit_a "- **Causa raíz**: ${D_CAUSE[idx]}"
        emit_a "- **Impacto**: ${D_IMPACT[idx]}"
        emit_a "- **Solución**: ${D_FIX[idx]}"
        if [[ -n "${D_CMD[idx]}" ]]; then
            emit_a ""
            emit_a '```bash'
            emit_a "${D_CMD[idx]}"
            emit_a '```'
        fi
        emit_a ""
    done
}

section_analysis() {
    msg "Análisis en profundidad"
    local deploy_key gw_key news_key extra route fish_key oa_base
    local news_url gw_port llm llm_code cmds diffs

    extra="$(read_env apps/gateway/.env TTS_EXTRA_ENGINES "")"
    route="$(read_env apps/gateway/.env TTS_ENGINES "")"
    fish_key="$(read_env apps/gateway/.env FISH_API_KEY "")"
    oa_base="$(read_env apps/gateway/.env OPENAI_TTS_BASE_URL "")"

    # --- 1. una misma clave en tres archivos -------------------------------
    if [[ -f deploy/.env || -f apps/news/.env || -f apps/gateway/.env ]]; then
        deploy_key="$(read_env deploy/.env GATEWAY_API_KEY "")"
        gw_key="$(read_env apps/gateway/.env API_KEY "")"
        news_key="$(read_env apps/news/.env TTS_API_KEY "")"
        diffs=()
        if [[ -n "$deploy_key" ]]; then
            [[ -n "$gw_key" && "$gw_key" != "$deploy_key" ]] && diffs+=("apps/gateway/.env API_KEY")
            [[ -n "$news_key" && "$news_key" != "$deploy_key" ]] && diffs+=("apps/news/.env TTS_API_KEY")
        elif [[ -n "$gw_key" && -n "$news_key" && "$gw_key" != "$news_key" ]]; then
            diffs+=("apps/gateway/.env API_KEY" "apps/news/.env TTS_API_KEY")
        fi

        if [[ ${#diffs[@]} -gt 0 ]]; then
            if [[ -n "$deploy_key" ]]; then
                cmds="$(cat <<'EOS'
KEY=$(grep -E '^GATEWAY_API_KEY=' deploy/.env | cut -d= -f2-)
sed -i "s|^TTS_API_KEY=.*|TTS_API_KEY=${KEY}|" apps/news/.env
sed -i "s|^API_KEY=.*|API_KEY=${KEY}|" apps/gateway/.env
EOS
)"
            else
                cmds="$(cat <<'EOS'
KEY=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
sed -i "s|^GATEWAY_API_KEY=.*|GATEWAY_API_KEY=${KEY}|" deploy/.env
sed -i "s|^TTS_API_KEY=.*|TTS_API_KEY=${KEY}|" apps/news/.env
sed -i "s|^API_KEY=.*|API_KEY=${KEY}|" apps/gateway/.env
EOS
)"
            fi
            deep bad "Claves del gateway incoherentes" \
                "El mismo secreto está en tres archivos y no coinciden: ${diffs[*]}" \
                "Cada .env guarda su propia copia de la clave y se rellenaron por separado (o se generó una nueva solo en uno de ellos). compose inyecta deploy/GATEWAY_API_KEY en el contenedor gateway, con prioridad sobre apps/gateway/.env." \
                "El gateway valida X-API-Key contra una única copia: news-engine recibe 401 en /tts, /voices/* y /engines, los digests degradan a Edge y el catálogo de voces no sincroniza." \
                "Igualar los tres valores (el asistente lo hace con «Unificar la clave del gateway» en la pantalla de validación, o ejecutando los comandos)." \
                "$cmds"
        elif [[ -z "$deploy_key" && -z "$gw_key" && -z "$news_key" ]]; then
            deep warn "Gateway sin API key" \
                "GATEWAY_API_KEY (deploy), API_KEY (gateway) y TTS_API_KEY (news) están vacías." \
                "Plantillas recién copiadas sin rellenar; el asistente solo genera el secreto cuando se pide." \
                "La API del gateway queda abierta: cualquiera que alcance el puerto publicado puede sintetizar voz y clonar voces a tu costa (en Shape B el puerto queda en la red/ Tailscale)." \
                "Generar una clave y copiarla a los tres archivos (el asistente la propaga sola al escribirla en cualquiera de los tres)." \
                'KEY=$(python3 -c "import secrets;print(secrets.token_hex(32))")' \
                'sed -i "s|^GATEWAY_API_KEY=.*|GATEWAY_API_KEY=${KEY}|" deploy/.env' \
                'sed -i "s|^TTS_API_KEY=.*|TTS_API_KEY=${KEY}|" apps/news/.env' \
                'sed -i "s|^API_KEY=.*|API_KEY=${KEY}|" apps/gateway/.env'
        else
            okc
        fi
    fi

    # --- 2. URL del gateway -------------------------------------------------
    if [[ -f apps/news/.env ]]; then
        news_url="$(read_env apps/news/.env TTS_API_BASE_URL "")"
        gw_port="$(read_env deploy/.env GATEWAY_PORT 8001)"
        if [[ -z "$news_url" ]]; then
            if [[ "$(read_env apps/news/.env TTS_DEFAULT_BACKEND edge)" != "edge" ]]; then
                deep bad "TTS_API_BASE_URL sin definir" \
                    "apps/news/.env no dice dónde está el gateway." \
                    "La plantilla se copió y no se completó la sección «TTS y gateway»." \
                    "TTS_DEFAULT_BACKEND=$(read_env apps/news/.env TTS_DEFAULT_BACKEND '?') necesita un gateway: la síntesis fallará con error de conexión." \
                    "Apuntar a la URL correcta según el despliegue." \
                    'sed -i "s|^TTS_API_BASE_URL=.*|TTS_API_BASE_URL=http://gateway:8000/api/v1|" apps/news/.env   # Shape A (Docker)' \
                    '# Shape B / host: TTS_API_BASE_URL=http://<gpu-host>:8001/api/v1'
            fi
        else
            case "$news_url" in
                http://gateway:8000|http://gateway:8000/*) okc ;;
                *:8000|*:8000/*)
                    deep bad "TTS_API_BASE_URL apunta al puerto 8000" \
                        "apps/news/.env apunta a ${news_url} (8000 es el puerto de la API news, el gateway publica ${gw_port})." \
                        "Se confundió el puerto interno del contenedor gateway (8000, el que usa la DNS interna http://gateway:8000) con el publicado en el host (GATEWAY_PORT=${gw_port})." \
                        "Toda llamada a /tts, /voices/* y /engines responde 404 o connection refused: ningún backend ≠ edge puede sintetizar." \
                        "Usar la DNS interna en Shape A o el puerto publicado fuera de Docker." \
                        "sed -i \"s|^TTS_API_BASE_URL=.*|TTS_API_BASE_URL=http://gateway:8000/api/v1|\" apps/news/.env   # Shape A (Docker)" \
                        "# sed -i \"s|^TTS_API_BASE_URL=.*|TTS_API_BASE_URL=http://localhost:${gw_port}/api/v1|\" apps/news/.env   # host"
                    ;;
                *)
                    url_port="$(printf '%s' "$news_url" | sed -E 's#^[a-z]+://[^:/]+:([0-9]+).*#\1#')"
                    if [[ "$url_port" =~ ^[0-9]+$ && "$url_port" != "$gw_port" && "$news_url" != *":${gw_port}/"* ]]; then
                        deep warn "Puerto de gateway distinto de GATEWAY_PORT" \
                            "TTS_API_BASE_URL=$news_url pero deploy/.env fija GATEWAY_PORT=${gw_port}." \
                            "Se cambió el puerto publicado en un archivo y no en el otro." \
                            "Si el gateway se publica en ${gw_port}, la URL no le alcanza (connection refused)." \
                            "Alinear el puerto de la URL con GATEWAY_PORT (o al revés)." \
                            "grep -E '^(GATEWAY_PORT|TTS_API_BASE_URL)=' deploy/.env apps/news/.env"
                    else
                        okc
                    fi
                    ;;
            esac
        fi
    fi

    # --- 3. engines cloud y sidecars ---------------------------------------
    if grep -q "fish_tts" <<<"$extra$route"; then
        if [[ -z "$fish_key" ]]; then
            deep warn "Fish habilitado sin credenciales" \
                "fish_tts aparece en TTS_EXTRA_ENGINES/TTS_ENGINES pero FISH_API_KEY está vacía." \
                "Se añadió el engine a la configuración sin dar de alta la cuenta de Fish Audio." \
                "En routed el engine queda inactivo (no degrada el health global), pero todo pin engine=fish_tts falla con 400 y sus voces no aparecen en /engines." \
                "Rellenar FISH_API_KEY (y revisar FISH_MODEL/FISH_API_BASE_URL) o quitar fish_tts de TTS_EXTRA_ENGINES." \
                "# sed -i 's|^FISH_API_KEY=.*|FISH_API_KEY=<tu clave de fish.audio>|' apps/gateway/.env"
        else
            okc
        fi
    fi
    if grep -q "openai_tts" <<<"$extra$route"; then
        if [[ -z "$oa_base" ]]; then
            deep warn "OpenAI-compatible habilitado sin endpoint" \
                "openai_tts aparece en TTS_EXTRA_ENGINES/TTS_ENGINES pero OPENAI_TTS_BASE_URL está vacía." \
                "Se copió la plantilla del proveedor sin apuntar a ningún servidor /audio/speech." \
                "El engine queda inactivo en routed; los pins engine=openai_tts devuelven 400 y la voz no aparece en /engines." \
                "Definir OPENAI_TTS_BASE_URL (incluyendo /v1) y OPENAI_TTS_API_KEY si el proveedor lo exige, o quitar openai_tts de los extras." \
                "# OPENAI_TTS_BASE_URL=https://api.openai.com/v1"
        else
            okc
        fi
    elif [[ -n "$oa_base" || -n "$fish_key" ]]; then
        deep warn "Credenciales cloud sin engine habilitado" \
            "Hay credenciales cloud configuradas pero el engine correspondiente no está en TTS_EXTRA_ENGINES." \
            "Se configuró el proveedor sin añadirlo a la ruta/extra del gateway." \
            "El proveedor no se instancia: sus voces siguen ocultas en el selector y /engines no lo reporta disponible." \
            "Añadir fish_tts u openai_tts a TTS_EXTRA_ENGINES (o rutear un idioma a él)." \
            "# TTS_EXTRA_ENGINES=kokoro,qwen3_tts,omnivoice_remote,fish_tts"
    fi

    # --- 3b. sidecars (títulos idénticos a la sección 5: solo aportan el
    # análisis, no repiten la línea del índice) ---------------------------
    local qwen_url omni_url running_side=""
    qwen_url="$(read_env deploy/.env QWEN3_TTS_REMOTE_URL "")"
    [[ -z "$qwen_url" ]] && qwen_url="$(read_env apps/gateway/.env QWEN3_TTS_REMOTE_URL "")"
    omni_url="$(read_env apps/gateway/.env OMNIVOICE_REMOTE_URL "")"
    if have docker; then
        running_side="$(docker compose -f "$COMPOSE_FILE" ps --status running --format '{{.Service}}' 2>/dev/null || true)"
    fi
    if grep -q "qwen3" <<<"$extra$route"; then
        if [[ -z "$qwen_url" ]]; then
            deep bad "qwen3_tts habilitado en el gateway sin QWEN3_TTS_REMOTE_URL (engine NOT_AVAILABLE, voces → Edge)" \
                "TTS_EXTRA_ENGINES/TTS_ENGINES mencionan qwen3_tts pero el gateway no tiene URL de sidecar." \
                "El sidecar se añadió a la configuración sin desplegarlo con QWEN3=1 (deploy/.env no define QWEN3_TTS_REMOTE_URL)." \
                "qwen3_tts queda NOT_AVAILABLE en /engines: los pines engine=qwen3_tts fallan y TTS_FALLBACK_TO_EDGE reescribe la síntesis con Edge, perdiendo la voz del catálogo." \
                "Publicar la URL del sidecar y arrancarlo con el perfil qwen3 y el overlay GPU." \
                "echo 'QWEN3_TTS_REMOTE_URL=http://gateway-qwen3:8000/api/v1' >> deploy/.env" \
                "GPU=1 QWEN3=1 ./scripts/deploy.sh production"
        elif ! grep -qx "gateway-qwen3" <<<"$running_side"; then
            deep bad "gateway-qwen3 configurado en QWEN3_TTS_REMOTE_URL pero el contenedor está parado" \
                "QWEN3_TTS_REMOTE_URL=$qwen_url pero el servicio gateway-qwen3 no corre." \
                "El perfil compose qwen3 no estaba activo en el último up (o el contenedor se cayó por un fallo de build/GPU)." \
                "El proxy del gateway no alcanza el sidecar: /engines lo marca no disponible y las voces qwen3 degradan a Edge." \
                "Arrancar el sidecar con su perfil y el overlay GPU." \
                "GPU=1 QWEN3=1 ./scripts/deploy.sh production" \
                "# docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.gpu.yml --profile qwen3 up -d"
        else
            okc
        fi
    fi
    if grep -q "omnivoice_remote" <<<"$extra$route"; then
        if [[ -z "$omni_url" ]]; then
            deep bad "omnivoice_remote habilitado sin OMNIVOICE_REMOTE_URL (engine no disponible)" \
                "omnivoice_remote está en TTS_EXTRA_ENGINES/TTS_ENGINES pero OMNIVOICE_REMOTE_URL está vacía." \
                "Se añadió el engine sin apuntar al contenedor gateway-omnivoice (que es el que instala las dependencias reales de OmniVoice)." \
                "El engine no se instancia: los clones con engine=omnivoice_remote no resuelven y la narración local falla." \
                "Definir la URL interna del sidecar y arrancarlo con su perfil." \
                "sed -i 's|^OMNIVOICE_REMOTE_URL=.*|OMNIVOICE_REMOTE_URL=http://gateway-omnivoice:8000|' apps/gateway/.env" \
                "docker compose -f deploy/docker-compose.yml --profile omnivoice up -d gateway-omnivoice"
        elif ! grep -qx "gateway-omnivoice" <<<"$running_side"; then
            deep bad "gateway-omnivoice parado con OMNIVOICE_REMOTE_URL configurada (\`docker compose --profile omnivoice up -d gateway-omnivoice\`)" \
                "OMNIVOICE_REMOTE_URL=$omni_url pero el servicio gateway-omnivoice no corre." \
                "El perfil compose omnivoice no estaba activo en el último up (o el contenedor se cayó)." \
                "Todo pin engine=omnivoice_remote y toda narración local que dependa de él responde con engine no disponible." \
                "Arrancar solo el sidecar (no levanta nada más)." \
                "docker compose -f deploy/docker-compose.yml --profile omnivoice up -d gateway-omnivoice"
        else
            okc
        fi
    fi

    # --- 4. LLM -------------------------------------------------------------
    if [[ -f apps/news/.env ]]; then
        llm="$(read_env apps/news/.env LLM_API_BASE "")"
        if [[ -z "$llm" ]]; then
            deep bad "LLM_API_BASE sin definir" \
                "apps/news/.env no define endpoint de LLM." \
                "Plantilla copiada sin completar la sección «LLM» (paso 3 del asistente)." \
                "Ningún workflow puede ejecutarse: el digest, la reescritura y la verificación LLM terminan en error." \
                "Apuntar al servidor local (llama.cpp/Ollama) con el modelo correcto." \
                "# sed -i 's|^LLM_API_BASE=.*|LLM_API_BASE=http://host.docker.internal:8080/v1|' apps/news/.env"
        fi
    fi

    # --- 5. secretos sin rellenar ------------------------------------------
    local stale="" k
    for k in TELEGRAM_CHAT_ID DOC_TTS_LINK_SECRET ADMIN_PASSWORD_HASH; do
        local v
        v="$(read_env apps/news/.env "$k" "")"
        if [[ "$v" == your_* || "$v" == changeme* || "$v" == "<"* ]]; then
            stale+="${stale:+, }$k"
        fi
    done
    if [[ -n "$stale" ]]; then
        deep warn "Secretos todavía en modo plantilla" \
            "Valores placeholder en apps/news/.env: $stale." \
            "Se copió .env.example sin sustituir los valores marcados como your_*/cambiar." \
            "Telegram no entrega boletines (token inválido), los enlaces de audio responden 503 si DOC_TTS_LINK_SECRET es placeholder y el acceso admin puede quedar débil." \
            "Sustituir cada placeholder por un valor real (el asistente genera los secretos con «g»)." \
            "grep -nE '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|DOC_TTS_LINK_SECRET)=' apps/news/.env"
    else
        [[ -f apps/news/.env ]] && okc
    fi

    # --- 7. stack ------------------------------------------------------------
    if have docker && docker compose -f "$COMPOSE_FILE" ps --format '{{.Service}}' >/dev/null 2>&1; then
        local up_list
        up_list="$(docker compose -f "$COMPOSE_FILE" ps --status running --format '{{.Service}}' 2>/dev/null || true)"
        if ! grep -qx "api" <<<"$up_list" || ! grep -qx "gateway" <<<"$up_list"; then
            deep bad "Stack sin desplegar (falta api o gateway)" \
                "Los servicios críticos no están en marcha (en marcha: ${up_list:-ninguno})." \
                "Nunca se ejecutó el despliegue en este host, o se paró con docker compose down." \
                "No hay panel, ni API, ni gateway TTS: nada responde en los puertos publicados." \
                "Desplegar el stack completo (idempotente)." \
                "GPU=1 ./scripts/deploy.sh production   # añade QWEN3=1 si usas qwen3_tts"
        else
            okc
        fi
    fi

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
section_analysis
render_analysis

HEADER="$(cat <<EOF
# newsvoice — informe de despliegue

> Generado: **$(date -Is 2>/dev/null || date)** · host: **$(hostname 2>/dev/null || echo '?')** · git: **$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')@$(git rev-parse --short HEAD 2>/dev/null || echo '?')**
> Propósito: pegar este Markdown en una herramienta AI para que indique **qué falta** y los comandos de remediación.
> El **análisis en profundidad** (justo tras los hallazgos) empareja cada problema con su causa raíz, su impacto y su solución.
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
    cat "$ANALYSIS"
    printf -- '---\n\n'
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
