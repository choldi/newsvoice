#!/usr/bin/env bash
# Ventana de diseño de voces (Fase 7.9b, maquina unica; lease en Fase P8).
#
# El diseño de voces necesita la GPU (Qwen3-TTS), asi que el gateway TTS y los
# modelos cargados de Ollama se detienen (se descargan de VRAM) mientras dura la
# ventana y se restauran al salir. El worker de diseño esta apagado por defecto:
# este script es la unica via para levantarlo; al arrancar carga el engine
# Qwen3-TTS VoiceDesign y ejecuta la creación de voces.
#
# Desde la Fase P8 la ventana entra en el lease de prioridad del motor: antes de
# tocar el gateway se pide el lease con prioridad máxima (diseño, 20), lo que
# hace que las sintesis en vuelo (digest 10, renders 0) cedan en lugar de morir
# al parar el gateway. El lease se libera al final, despues de restaurarlo.
#
# Uso:  ./scripts/design_window.sh [timeout_s]
# Env:  TTS_GATEWAY_UNIT   unit del gateway (por defecto autodetecta
#                          tts-api.service y omnivoice-api.service)
#       DESIGN_WINDOW_POLL    segundos entre comprobaciones (15)
#       DESIGN_WINDOW_TIMEOUT tiempo de espera de workflows (3600)
#       DESIGN_WINDOW_LEASE_TIMEOUT  espera del lease (900)
#       DESIGN_WINDOW_NO_LEASE=1     salta el lease (Shape B: GPU box sin
#                                    data/news.db compartida)
#       NEWS_PYTHON      interprete del venv de news (por defecto $PROJECT_DIR/.venv/bin/python)
#       TTS_WAS_ACTIVE  1|0  fuerza si el gateway debe restaurarse
set -euo pipefail

NAMESPACE="${VOICE_NAMESPACE:-news-engine-voices}"
TASK_QUEUE="${VOICE_DESIGN_QUEUE:-news-engine-voice-design-queue}"
TIMEOUT_S="${1:-${DESIGN_WINDOW_TIMEOUT:-3600}}"
POLL_S="${DESIGN_WINDOW_POLL:-15}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

NEWS_PYTHON="${NEWS_PYTHON:-$PROJECT_DIR/.venv/bin/python}"
if [[ ! -x "$NEWS_PYTHON" ]]; then
  NEWS_PYTHON="python3"
fi

LEASE_OWNER="design-window"
LEASE_WAIT_TIMEOUT="${DESIGN_WINDOW_LEASE_TIMEOUT:-900}"
LEASE_PID=""

log() { printf '[design_window] %s\n' "$*"; }

design_lease() {
  # Se ejecuta con cwd = PROJECT_DIR para resolver data/news.db y .env.
  ( cd "$PROJECT_DIR" && "$NEWS_PYTHON" -m src.workers.design_lease "$@" )
}

lease_held() {
  design_lease status 2>/dev/null |
    "$NEWS_PYTHON" -c 'import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
holder = data.get("holder") or {}
sys.exit(0 if holder.get("note") == "design-window" else 1)'
}

unit_exists() {
  systemctl --user list-unit-files --type=service --no-legend 2>/dev/null |
    awk '{print $1}' | grep -qx "$1"
}

resolve_gateway_unit() {
  # The gateway was rebranded omnivoice-api -> tts-api; the deployed unit name
  # is not guaranteed, so accept an override and fall back to both names.
  if [[ -n "${TTS_GATEWAY_UNIT:-}" ]]; then
    printf '%s' "$TTS_GATEWAY_UNIT"
    return
  fi
  local unit
  for unit in tts-api.service omnivoice-api.service; do
    if unit_exists "$unit"; then
      printf '%s' "$unit"
      return
    fi
  done
  printf ''
}

GATEWAY_UNIT="$(resolve_gateway_unit)"
if [[ -z "$GATEWAY_UNIT" ]]; then
  log "ERROR: no encuentro la unit del gateway (tts-api.service ni omnivoice-api.service)"
  log "define TTS_GATEWAY_UNIT=<unit> si usa otro nombre"
  exit 1
fi
log "gateway: $GATEWAY_UNIT"

temporal_cli() {
  docker compose -f "$PROJECT_DIR/docker-compose.yml" exec -T temporal temporal "$@"
}

running_workflows() {
  temporal_cli workflow list \
    --address temporal:7233 \
    --namespace "$NAMESPACE" \
    --query "ExecutionStatus='Running'" \
    --output json 2>/dev/null |
    python3 -c 'import json, sys; print(len(json.load(sys.stdin)))'
}

pollers() {
  temporal_cli task-queue describe \
    --address temporal:7233 \
    --namespace "$NAMESPACE" \
    --task-queue "$TASK_QUEUE" \
    --output json 2>/dev/null |
    python3 -c 'import json, sys; print(len(json.load(sys.stdin).get("pollers") or []))'
}

if [[ -z "${TTS_WAS_ACTIVE:-}" ]]; then
  if systemctl --user is-active "$GATEWAY_UNIT" >/dev/null 2>&1; then
    TTS_WAS_ACTIVE=1
  else
    TTS_WAS_ACTIVE=0
  fi
fi

cleanup() {
  local rc=$?
  log "cerrando la ventana (exit=$rc)"
  systemctl --user stop news-engine-voice-worker.service || true
  if [[ "$TTS_WAS_ACTIVE" == "1" ]]; then
    log "restaurando el gateway ($GATEWAY_UNIT)"
    systemctl --user start "$GATEWAY_UNIT" || true
  fi
  if [[ -n "$LEASE_PID" ]] && kill -0 "$LEASE_PID" 2>/dev/null; then
    # Liberar el lease DESPUES de restaurar el gateway: si no, un digest que
    # espera el turno podria arrancar contra un gateway parado.
    log "liberando el lease de GPU"
    kill -TERM "$LEASE_PID" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$LEASE_PID" 2>/dev/null || break
      sleep 0.5
    done
    kill -KILL "$LEASE_PID" 2>/dev/null || true
    wait "$LEASE_PID" 2>/dev/null || true
  fi
  log "Ollama sigue como servicio; sus modelos quedan descargados de VRAM"
}
trap cleanup EXIT

if [[ "${DESIGN_WINDOW_NO_LEASE:-0}" == "1" ]]; then
  log "0/5 DESIGN_WINDOW_NO_LEASE=1: ventana sin lease (Shape B, sin data/news.db compartida)"
else
  log "0/5 pidiendo el lease de GPU (prioridad diseño, espera ${LEASE_WAIT_TIMEOUT}s)"
  ( cd "$PROJECT_DIR" && exec "$NEWS_PYTHON" -m src.workers.design_lease open \
      --wait-timeout "$LEASE_WAIT_TIMEOUT" ) &
  LEASE_PID=$!
  lease_deadline=$((SECONDS + LEASE_WAIT_TIMEOUT + 30))
  until lease_held; do
    if ! kill -0 "$LEASE_PID" 2>/dev/null; then
      wait "$LEASE_PID" 2>/dev/null || true
      log "ERROR: la CLI del lease terminó sin adquirirlo (timeout o conflicto)"
      LEASE_PID=""
      exit 1
    fi
    if ((SECONDS > lease_deadline)); then
      log "ERROR: el lease no se adquirió en ${LEASE_WAIT_TIMEOUT}s"
      exit 1
    fi
    sleep 2
  done
  log "lease adquirido por $LEASE_OWNER"
fi

log "1/5 parando el gateway ($GATEWAY_UNIT) para liberar VRAM"
systemctl --user stop "$GATEWAY_UNIT" || true

if command -v ollama >/dev/null 2>&1; then
  log "2/5 descargando de la VRAM los modelos de Ollama"
  while read -r model; do
    [[ -n "$model" ]] && ollama stop "$model" || true
  done < <(ollama ps 2>/dev/null | tail -n +2 | awk '{print $1}')
else
  log "2/5 Ollama no esta instalado, no hay modelos que descargar"
fi

log "3/5 arrancando el worker de diseño (queue=$TASK_QUEUE)"
systemctl --user start news-engine-voice-worker.service

log "4/5 esperando al poller del worker de diseño"
deadline=$((SECONDS + 300))
until [[ "$(pollers)" -gt 0 ]]; do
  if ((SECONDS > deadline)); then
    log "ERROR: el worker de diseño no registró poller en 300s"
    exit 1
  fi
  sleep "$POLL_S"
done

log "5/5 esperando a los workflows de la namespace $NAMESPACE"
deadline=$((SECONDS + TIMEOUT_S))
while true; do
  n="$(running_workflows)"
  if [[ "$n" == "0" ]]; then
    log "no quedan workflows en curso"
    break
  fi
  if ((SECONDS > deadline)); then
    log "AVISO: timeout tras ${TIMEOUT_S}s con $n workflow(s) en curso"
    log "el trap de salida los deja terminar; revisa el journal del worker"
    break
  fi
  log "  $n workflow(s) en curso..."
  sleep "$POLL_S"
done
