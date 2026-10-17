---
feature: omnivoice-full-integration
status: delivered
updated: 2026-09-09
branch: feature/omnivoice-full-integration
commits: 079e41c..HEAD
---

# OmniVoice Full Integration

## Report

## [S1] Problem

The API currently exposes only a subset of OmniVoice's capabilities: stock voice IDs mapped to instructs, speed control, and basic voice cloning. Key features documented by OmniVoice are missing or broken:

- **Emotions are non-functional**: The model rejects emotion tokens ("happy", "sad") as invalid instructs, causing 500 errors.
- **No generation quality control**: Users cannot tune `num_step`, `denoise`, or `guidance_scale`.
- **No duration control**: Only speed is exposed; fixed-duration output is unavailable.
- **No audio post-processing control**: Silence trimming, fade in/out are not configurable.
- **No long-form support**: Long texts cannot be chunked for stable generation with constant VRAM.
- **No custom instruct**: Users must use stock voice IDs; free-form voice design is impossible.
- **No ref_audio + instruct combo**: Voice cloning cannot be enhanced with instruct attributes.

## [S2] Design

### Overview

Expose the full OmniVoice `model.generate()` parameter surface through the API, add a custom instruct endpoint, enable ref_audio+instruct combination for cloning, remove the non-functional emotion feature, and improve error responses with allowed values.

### S2.1 Remove Emotion Feature

- Remove `emotion` and `intensity` parameters from:
  - `POST /api/v1/tts` endpoint
  - `TtsService.synthesize_stock()` and `synthesize_clone()`
  - `OmniVoiceEngine.synthesize_stock()` and `synthesize_clone()`
- Remove `_emotion_to_instruct()` and `_build_instruct()` emotion logic from engine
- Remove `UnsupportedEmotionError` exception class
- Remove `list_emotions()` from engine, engine client, and endpoint
- Remove `emotions` router/endpoint if it exists
- Return 400 with clear message if legacy clients send `emotion` param

### S2.2 Generation Parameters

Add optional parameters to `POST /api/v1/tts`:

| Parameter | Type | Default | Range | Description |
|---|---|---|---|---|
| `num_step` | int | 32 | 1-100 | Unmasking steps. Higher = better quality, slower |
| `denoise` | bool | True | - | Prepend denoise token for cleaner speech |
| `guidance_scale` | float | 2.0 | 0.0-10.0 | Classifier-free guidance scale |
| `duration` | float \| null | null | 0.5-120.0 | Fixed output duration in seconds. Overrides speed |
| `preprocess_prompt` | bool | True | - | Preprocess voice-clone prompt audio |
| `postprocess_output` | bool | True | - | Remove long silences from output |
| `pad_duration` | float | 0.1 | 0.0-1.0 | Silence padding per side (seconds) |
| `fade_duration` | float | 0.1 | 0.0-1.0 | Fade-in/out duration (seconds) |

Priority rule: `duration` > `speed`. When `duration` is set, `speed` is ignored.

Pass these through `engine_client` -> `engine` -> `model.generate()`.

### S2.3 Custom Instruct Endpoint

Add `POST /api/v1/tts/instruct` for advanced users who want free-form voice design:

```
POST /api/v1/tts/instruct
{
  "text": "Hello world",
  "instruct": "female, young adult, high pitch, british accent",
  "speed": 1.0,
  "num_step": 32,
  "denoise": true,
  "guidance_scale": 2.0
}
```

- Validate `instruct` tokens against `VALID_INSTRUCT_TOKENS_EN`
- Return 400 with list of valid tokens if invalid
- No stock voice lookup; instruct is used directly

### S2.4 ref_audio + Instruct Combo

Modify `POST /api/v1/tts` (voice cloning path) to accept optional `instruct`:

```
POST /api/v1/tts
{
  "text": "Hola mundo",
  "voice_id": "cloned-voice-123",
  "language": "es",
  "instruct": "male, portuguese accent",
  "speed": 1.0
}
```

- When `voice_id` matches a cloned voice AND `instruct` is provided, pass both `ref_audio` and `instruct` to `model.generate()`
- Per OmniVoice docs: "instruct can improve cloning stability for the attributes it describes"
- Validate `instruct` tokens if provided

### S2.5 Long-Form Generation

Add optional long-form parameters to both endpoints:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `audio_chunk_duration` | float | 15.0 | Target chunk duration (seconds) |
| `audio_chunk_threshold` | float | 30.0 | Duration above which chunking activates |

Pass through to `model.generate()`. The model handles text splitting internally.

### S2.6 Voice Design Info Endpoint

Add `GET /api/v1/tts/voice-design/tokens` to expose valid instruct tokens:

```json
{
  "gender": ["male", "female"],
  "age": ["child", "teenager", "young adult", "middle-aged", "elderly"],
  "pitch": ["very low pitch", "low pitch", "moderate pitch", "high pitch", "very high pitch"],
  "style": ["whisper"],
  "english_accent": ["american accent", "australian accent", "british accent", ...],
  "chinese_dialect": ["河南话", "陕西话", "四川话", ...]
}
```

### S2.7 Improved Error Responses

All validation errors return structured JSON:

```json
{
  "detail": "Instruct inválido: 'Mexican Spanish accent'",
  "error_type": "unsupported_instruct",
  "invalid_items": [{"token": "mexican spanish accent", "suggestion": null}],
  "valid_tokens": ["male", "female", "american accent", ...]
}
```

For stock voice requests with invalid voice_id:

```json
{
  "detail": "Voz no encontrada: xx-male",
  "error_type": "voice_not_found",
  "valid_voice_ids": ["es-mx-male", "es-mx-female", ...]
}
```

### Architecture Changes

```
API Layer (tts.py)
  ├── POST /tts           (stock voices — instruct from voice_id)
  ├── POST /tts/instruct  (custom instruct — free-form)
  └── GET /tts/voice-design/tokens

Service Layer (tts.py)
  ├── synthesize_stock()     — validates voice_id, builds instruct from mapping
  ├── synthesize_instruct()  — validates instruct tokens directly
  └── synthesize_clone()     — validates ref_audio, optional instruct combo

Engine Client (engine_client.py)
  ├── synthesize_stock(text, voice_id, speed, generation_params)
  ├── synthesize_instruct(text, instruct, speed, generation_params)
  └── synthesize_clone(text, ref_audio, instruct?, speed, generation_params)

Engine (omnivoice_engine.py)
  └── model.generate(text, instruct?, ref_audio?, speed, duration, **generation_params)
```

## Report

**What was built** — Full integration of OmniVoice's documented capabilities into the REST API. Removed the non-functional emotion feature (which caused 500 errors from invalid instruct tokens). Added `GenerationParams` dataclass exposing all OmniVoice generation parameters (num_step, denoise, guidance_scale, duration, pre/post processing, long-form chunking). Added `POST /api/v1/tts/instruct` for free-form voice design. Enabled ref_audio + instruct combination for cloning stability. Added `GET /api/v1/tts/voice-design/tokens` endpoint. Improved all error responses with structured JSON (error_type, valid values, suggestions).

**Verification** — `pytest tests/test_omnivoice_engine.py tests/test_engine_client.py tests/test_tts_service.py tests/test_tts_service_extended.py tests/test_tts_api.py` — 53 passed, 0 failed.

**Journey log** — Stock voice instructs were fixed from invalid tokens (e.g., "Mexican Spanish accent") to valid OmniVoice tokens (e.g., "male, portuguese accent"). Chinese tokens (男, 女) required extending the validation to support both English and Chinese instruct token sets.

## [S3] Out of Scope

- MP3 output format (currently returns WAV; MP3 conversion is a separate feature)
- Batch synthesis (multiple texts in one request)
- Streaming audio output
- WebSocket-based real-time synthesis
- Voice cloning creation/upload endpoints (existing, not modified)
- Min Nan Chinese (Hokkien) Tai-lo romanization support
- Chinese dialect instruct validation (separate from English tokens)

## Tasks

- [x] T1: Remove emotion feature from engine, service, and API layer — acceptance: `emotion` param returns 400, no `UnsupportedEmotionError` class exists, `list_emotions` endpoint removed (covers: S2.1)
- [x] T2: Add `GenerationParams` dataclass to engine client — acceptance: dataclass holds all generation params with defaults matching OmniVoice docs, passes kwargs to `model.generate()` (covers: S2.2)
- [x] T3: Expose generation parameters in `POST /api/v1/tts` endpoint — acceptance: all params from S2.2 appear in OpenAPI schema, `duration` overrides `speed` when both set (covers: S2.2)
- [x] T4: Add `POST /api/v1/tts/instruct` endpoint for custom instruct — acceptance: accepts free-form `instruct` string, validates tokens, returns 400 with valid tokens on invalid instruct (covers: S2.3)
- [x] T5: Add optional `instruct` param to voice cloning path — acceptance: when `voice_id` is cloned AND `instruct` provided, both `ref_audio` and `instruct` passed to `model.generate()` (covers: S2.4)
- [x] T6: Add long-form generation params — acceptance: `audio_chunk_duration` and `audio_chunk_threshold` params exposed in both endpoints, passed to engine (covers: S2.5)
- [x] T7: Add `GET /api/v1/tts/voice-design/tokens` endpoint — acceptance: returns JSON with all valid instruct tokens grouped by category (covers: S2.6)
- [x] T8: Improve error response structure — acceptance: all validation errors return `error_type` and suggestion fields (covers: S2.7)
- [x] T9: Update tests for all changes — acceptance: all new endpoints have integration tests, removed emotion paths have no test references, generation params tested (covers: S2.1-S2.7)
