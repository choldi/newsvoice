#!/usr/bin/env bash
# Ventana de diseño de voces (Fase 7.9b, maquina unica).
#
# El diseño de voces necesita la GPU (Qwen3-TTS), asi que OmniVoice y los
# modelos cargados de Ollama se detienen mientras dura la ventana y se
# restauran al salir. El worker de diseño esta apagado por defecto: este
# script es la unica via para levantarlo.
#
# Uso:  ./scripts/design_window.sh [timeout_s]
# Env:  DESIGN_WINDOW_POLL    segundos entre comprobaciones (15)
#       TTS_WAS_ACTIVE  1|0  fuerza si OmniVoice debe restaurarse
set -euo pipefail

NAMESPACE="${VOICE_NAMESPACE:-news-engine-voices}"
TASK_QUEUE="${VOICE_DESIGN_QUEUE:-news-engine-voice-design-queue}"
TIMEOUT_S="${1:-${DESIGN_WINDOW_TIMEOUT:-3600}}"
POLL_S="${DESIGN_WINDOW_POLL:-15}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '[design_window] %s\n' "$*"; }

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
  if systemctl --user is-active tts-api.service >/dev/null 2>&1; then
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
    log "restaurando OmniVoice"
    systemctl --user start tts-api.service || true
  fi
  log "Ollama sigue como servicio; sus modelos quedan descargados de VRAM"
}
trap cleanup EXIT

log "1/5 parando OmniVoice para liberar VRAM"
systemctl --user stop tts-api.service || true

if command -v ollama >/dev/null 2>&1; then
  log "2/5 descargando modelos cargados de Ollama"
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
