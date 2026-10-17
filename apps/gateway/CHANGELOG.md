# Changelog

Formato: [Keep a Changelog](https://keepachangelog.com/es/1.1.0/)

## [0.8.0] - 2026-09-25

### Added
- **Multi-engine voice cloning**: parámetro `engines` en `POST /voices/clone` (ej: `engines=pocket_tts,omnivoice`)
- Columna `engine` en tabla `cloned_voices` (lista separada por comas) con migración automática
- Filtro `?engine=` en `GET /voices/cloned` (usa LIKE para voces multi-engine)
- `FeatureNotSupportedError` para voces clonadas de engine incorrecto (HTTP 400)
- **Auto-transcripción** de audio de referencia con `faster-whisper` (opcional, `AUTO_TRANSCRIBE=true`)
- `AudioValidator` engine-aware: 24000Hz para pocket_tts, 22050Hz para omnivoice
- `OmniVoiceAdapter` resamplea referencia 24kHz→22.05kHz en tiempo de síntesis

## [0.7.0] - 2026-09-20

### Added
- **Multi-engine TTS**: soporte para Pocket TTS, EdgeTTS y OmniVoice bajo una misma API
- `TtsEngineBase` ABC + `EngineCapabilities` (sistema de capacidades por engine)
- `engine_factory.py` con selección via `TTS_ENGINE` env var
- **PocketTTSEngine** (`pocket-tts`): CPU, clonado de voz, es/en/fr, 24000Hz
- **EdgeTTSEngine** (`edge-tts`): cloud, 400+ voces incluyendo catalán (ca-ES), sin clonado
- **MockEngine**: tonos de prueba (extraído de OmniVoiceEngine)
- **RoutedEngine**: enrutado por idioma con `TTS_ENGINES` JSON (ej: es→pocket_tts, ca→edgetts)
- `OmniVoiceAdapter`: envuelve el engine OmniVoice legacy en la nueva interfaz
- Makefile: `ENGINE` variable (`make install ENGINE=pocket_tts`)
- pyproject.toml: dependencias opcionales `[omnivoice]`, `[pocket_tts]`, `[edgetts]`, `[all-engines]`
- `FeatureNotSupportedError` para features no soportadas por un engine

### Changed
- torch/torchaudio movidos de dependencias base a grupo opcional `[omnivoice]`
- API endpoints refactorizados para ser engine-agnostic (sin GenerationParams en request)
- Health endpoint muestra `engine` activo (ej: `routed(edgetts, pocket_tts)`)
- `engine_client.py` refactorizado para aceptar cualquier `TtsEngineBase`

## [0.6.0] - 2026-09-12

### Added
- Emociones y singing via `ModelsLab/omnivoice-singing` (drop-in replacement de k2-fsa/OmniVoice)
- Tags de emoción soportados: `happy`, `sad`, `angry`, `excited`, `calm`, `nervous`, `whisper`, `singing`
- Parámetro `emotion` en `POST /api/v1/tts` y `POST /api/v1/tts/instruct`
- Endpoint `GET /api/v1/emotions` — lista emociones soportadas
- Variable de entorno `OMNIVOICE_MODEL_ID` configurable (default: `ModelsLab/omnivoice-singing`)

### Changed
- Modelo por defecto cambiado de `k2-fsa/OmniVoice` a `ModelsLab/omnivoice-singing`
- Documentación de API actualizada (api_spec_simplified.md) con endpoints de emociones y parámetros de generación

## [0.5.0] - 2025-01-XX

### Added
- Streaming WAV por chunks (?stream=true) en TTS y conversaciones
- Guía de despliegue (docs/deployment/)
- Stress tests con locust (tests/load/)

### Changed
- PhasesStatus.md actualizado con estado real de todos los sprints

## [0.4.0] - 2025-01-XX

### Added
- EnginePool con semáforo para concurrencia controlada
- Limpieza automática de outputs caducados (background task)
- Métricas Prometheus (prometheus_fastapi_instrumentator)
- Rate limiting con slowapi
- Logs estructurados con structlog + request_id
- API key opcional (APIKeyMiddleware)
- CORS configurable (CORS_ORIGINS)

## [0.3.0] - 2025-01-XX

### Added
- Endpoint POST /api/v1/conversations para conversaciones multi-voz
- ConversationService con concatenación WAV y silencios
- Tests de integración para conversaciones

## [0.2.0] - 2025-01-XX

### Added
- Voice cloning: POST /api/v1/voices/clone
- Voice CRUD: GET/DELETE /api/v1/voices/cloned/{id}
- VoiceService con validación de audio
- SQLite para persistencia de voces clonadas
- core/audio.py con validación y normalización
- GenerationParams para control fino de síntesis
- Voice design tokens endpoint
- Instruct API para voice design libre
- Clone + instruct combo

### Removed
- Emociones (Sprint 4) — removidas por diseño, integradas en engine

## [0.1.0] - 2025-01-XX

### Added
- MVP: TTS con voces stock
- GET /api/v1/voices/stock
- POST /api/v1/tts
- OmniVoiceEngine con fallback a mock
- Health endpoints (/health, /health/live, /health/ready)
- Tests unitarios e integración
- Dockerfile
