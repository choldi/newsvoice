#!/usr/bin/env bash
# Example: clone a voice using curl
# Host: 192.168.5.4
# Reference audio: Mel.wav (female, English UK)
# Transcription: Mel.txt (for reference, not sent in this request)

HOST="http://192.168.5.4:8000"  # adjust port if needed
API_ENDPOINT="${HOST}/api/v1/voices/clone"

curl -X POST "${API_ENDPOINT}" \
  -F "reference=@Mel.wav;type=audio/wav" \
  -F "name=MelVoiceUK" \
  -F "language=en-GB" \
  -H "Accept: application/json"

# Optional: if API key is required
# -H "X-API-Key: YOUR_API_KEY"

echo
