# Estado de las Fases

## Sprint 0 — Setup ✅
- [x] pyproject.toml con dependencias pinned
- [x] settings.py con pydantic-settings
- [x] main.py mínimo con /health
- [x] conftest.py base + fixture de cliente async
- [x] Makefile con make test, make lint, make run
- [x] Validar GPU detectada por PyTorch (torch.cuda.is_available())
- [x] README con quickstart
- [x] Variable de entorno OMNIVOICE_PATH para localizar instalación externa
- [x] Módulo omnivoice_api.core con engine_paths, engine_client, exceptions
- [x] /api/v1/health, /api/v1/health/live, /api/v1/health/ready operativos
- [x] Tests smoke verdes

## Sprint 1 — MVP: TTS con voz stock ✅ 🎯 MVP
- [x] OmniVoiceEngineInterface (Protocol)
- [x] OmniVoiceEngine concreto (singleton, warm-up)
- [x] TtsService.synthesize_stock(...)
- [x] Router tts.py + voices.py (sólo stock)
- [x] Manejo de UnsupportedLanguage, VoiceNotFound
- [x] Tests unitarios del servicio (engine mockeado)
- [x] Tests de integración con audio real generado
- [ ] Ejemplo en docs/examples/curl_tts_stock.sh

## Sprint 2 — Clonado de voces ⚠️
- [x] SQLite + inicialización de BD
- [x] VoiceRepository CRUD
- [x] VoiceService.clone() + synthesize_clone
- [x] core/audio.py: validación con soundfile
- [x] Tests con audio fixture (tests/fixtures/ref.wav)
- [x] Caché de embeddings (LRU)
- [x] Ejemplo Python docs/examples/clone_voice.py

## Sprint 3 — Conversaciones multi-voz ✅
- [x] ConversationService.generate(turns, pause_ms)
- [x] Concatenación WAV con silencios (stdlib wave)
- [x] Validación: 2+ turnos, voces existen, texto no vacío
- [x] Tests unitarios e integración
- [ ] Ejemplo docs/examples/dialogue.py

## Sprint 4 — Emociones ✅ (vía ModelsLab/omnivoice-singing)
- [x] Modelo ModelsLab/omnivoice-singing con tags de emoción y singing
- [x] Parámetro `emotion` en POST /tts y POST /tts/instruct
- [x] Endpoint GET /api/v1/emotions
- [x] Validación de emociones soportadas (400 con lista de válidas)
- [x] Tests actualizados

## Sprint 5 — Rendimiento y robustez ✅
- [x] EnginePool con semáforo (core/engine_pool.py)
- [x] Streaming de WAV por chunks (StreamingResponse)
- [x] Limpieza de outputs caducados (core/cleanup.py)
- [x] Métricas Prometheus (prometheus_fastapi_instrumentator)
- [x] Rate limiting (slowapi)
- [x] Logs estructurados con request_id (structlog + RequestIDMiddleware)
- [x] Stress test con locust en tests/load/

## Sprint 6 — Seguridad y DX ✅
- [x] API key opcional (APIKeyMiddleware)
- [x] CORS configurable (CORSMiddleware + settings.CORS_ORIGINS)
- [x] OpenAPI enriquecida con ejemplos en descriptions
- [x] Dockerfile (Linux)
- [x] Guía de despliegue docs/deployment/
- [x] CHANGELOG y semver
