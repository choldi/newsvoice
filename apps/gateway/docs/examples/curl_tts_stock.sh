#!/usr/bin/env bash
# Example: TTS with stock voices using curl
#
# Prerequisites:
#   - OmniVoice API server running (make run)
#
# Usage:
#   bash docs/examples/curl_tts_stock.sh

set -euo pipefail

API_BASE="http://localhost:8000/api/v1"

echo "=== 1. Health check ==="
curl -s "$API_BASE/health" | python3 -m json.tool

echo ""
echo "=== 2. List stock voices ==="
curl -s "$API_BASE/voices/stock" | python3 -m json.tool

echo ""
echo "=== 3. Synthesize Spanish (Mexico) ==="
curl -s -X POST "$API_BASE/tts" \
  -H "Content-Type: application/json" \
  -d '{"text": "Hola, bienvenido a la API de OmniVoice.", "voice_id": "es-mx-male", "language": "es"}' \
  --output output_es_mx.wav
echo "Saved: output_es_mx.wav ($(stat -c%s output_es_mx.wav 2>/dev/null || stat -f%z output_es_mx.wav) bytes)"

echo ""
echo "=== 4. Synthesize English (US) ==="
curl -s -X POST "$API_BASE/tts" \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello, welcome to the OmniVoice API.", "voice_id": "en-us-female", "language": "en"}' \
  --output output_en_us.wav
echo "Saved: output_en_us.wav ($(stat -c%s output_en_us.wav 2>/dev/null || stat -f%z output_en_us.wav) bytes)"

echo ""
echo "=== 5. Synthesize with custom instruct (voice design) ==="
curl -s -X POST "$API_BASE/tts/instruct" \
  -H "Content-Type: application/json" \
  -d '{"text": "This is a custom voice design example.", "instruct": "female, young adult, british accent", "language": "en"}' \
  --output output_instruct.wav
echo "Saved: output_instruct.wav ($(stat -c%s output_instruct.wav 2>/dev/null || stat -f%z output_instruct.wav) bytes)"

echo ""
echo "=== Done! ==="
echo "Files created:"
ls -la output_*.wav
