#!/usr/bin/env bash
# Test TTS with the first cloned voice available.
# Usage: ./scripts/test_cloned_tts.sh [text] [language]
#   text     - Text to synthesize (default: "Hello, this is a test of the cloned voice.")
#   language - Language code (default: auto-detected from the cloned voice)

set -euo pipefail

BASE_URL="${API_BASE_URL:-http://192.168.5.4:8000}"
TEXT="${1:-Hello, this is a test of the cloned voice. I hope you can hear the difference.}"
OUTPUT="/tmp/cloned_tts_test.wav"

echo "=== Cloned Voice TTS Test ==="
echo ""

# 1. List cloned voices
echo "1. Listing cloned voices..."
VOICES=$(curl -sf "${BASE_URL}/api/v1/voices/cloned")
COUNT=$(echo "$VOICES" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")

if [ "$COUNT" -eq 0 ]; then
    echo "   ERROR: No cloned voices found. Clone one first."
    exit 1
fi
echo "   Found ${COUNT} cloned voice(s):"
echo "$VOICES" | python3 -c "
import sys, json
for v in json.load(sys.stdin):
    print(f\"   - {v['name']} (id={v['id'][:8]}...) lang={v['language']} dur={v['duration_sec']:.1f}s\")
"

# 2. Get first cloned voice
VOICE_ID=$(echo "$VOICES" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")
VOICE_NAME=$(echo "$VOICES" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['name'])")
VOICE_LANG=$(echo "$VOICES" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['language'])")
LANG="${2:-$VOICE_LANG}"

echo ""
echo "2. Using voice: ${VOICE_NAME} (id=${VOICE_ID})"
echo "   Language: ${LANG}"
echo "   Text: \"${TEXT}\""

# 3. Synthesize (estimate duration: ~0.08s per char at speed 1.0, min 3s)
DURATION=$(python3 -c "print(max(3.0, int(len('${TEXT}') * 0.08 / 1.0) + 2))")
echo ""
echo "3. Synthesizing (duration=${DURATION}s)..."
HTTP_CODE=$(curl -sf -o "${OUTPUT}" -w "%{http_code}" \
    -X POST "${BASE_URL}/api/v1/tts" \
    -H "Content-Type: application/json" \
    -d "{\"text\":\"${TEXT}\", \"voice_id\":\"${VOICE_ID}\", \"language\":\"${LANG}\", \"duration\":${DURATION}}")

if [ "$HTTP_CODE" -ne 200 ]; then
    echo "   ERROR: HTTP ${HTTP_CODE}"
    cat "${OUTPUT}" 2>/dev/null | python3 -m json.tool 2>/dev/null || cat "${OUTPUT}"
    exit 1
fi

# 4. Verify output
echo ""
echo "4. Output saved to: ${OUTPUT}"
ls -lh "${OUTPUT}"
file "${OUTPUT}" 2>/dev/null || true

# 5. Show WAV info
python3 -c "
import wave
with wave.open('${OUTPUT}') as wf:
    dur = wf.getnframes() / wf.getframerate()
    print(f'   WAV: {wf.getnchannels()}ch, {wf.getframerate()}Hz, {dur:.1f}s, {wf.getnframes()} frames')
" 2>/dev/null || true

echo ""
echo "=== Done! Play with: aplay ${OUTPUT} ==="
