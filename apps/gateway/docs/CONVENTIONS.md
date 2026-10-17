# CONVENTIONS.md

> **EN:** Code conventions for the multi-engine TTS API.

## 1. Stack tecnológico

- Python: 3.11+
- Framework HTTP: FastAPI + Uvicorn (asyncio)
- **TTS Engines** (multi-engine, seleccionable via `TTS_ENGINE`):
  - Pocket TTS (`pocket_tts`) — CPU, clonado de voz, es/en/fr
  - EdgeTTS (`edgetts`) — Cloud, 400+ voces incl. catalán
  - OmniVoice (`omnivoice`) — GPU, emociones, voice design
  - Mock (`mock`) — Tonos de prueba
- Validación: Pydantic v2
- Persistencia: SQLite (metadatos) + filesystem (audio)
- Testing: pytest + pytest-asyncio + httpx (AsyncClient) + pytest-cov
- Calidad: ruff (lint+format), mypy (strict en src/)
- Empaquetado: uv + pyproject.toml (dependencias opcionales por engine)
- Audio nativo: binario de sistema **ffmpeg** (resample y time-stretch).
  Prohibido `librosa` (ver sección 11)

## 2. Estilo de código

- black con line-length = 100
- isort con perfil black
- ruff con reglas: E, F, I, B, UP, SIM, RET, PL
- Tipado estático obligatorio en toda función pública
- Docstrings estilo Google en módulos, clases y funciones públicas
- Nomenclatura:
  - PascalCase clases, snake_case funciones/variables, UPPER_SNAKE constantes
  - Enums en SCREAMING_SNAKE
  - Endpoints en kebab-case: /api/v1/voices/cloned

## 3. Estructura por capas

api → services → repositories → core(engines) → models

- La capa api no importa engines directamente.
- La capa core es la única que sabe hablar con los engines (via `TtsEngineBase`).
- `engine_factory.create_engine()` instancia el engine según `TTS_ENGINE`.
- services orquesta casos de uso, no conoce HTTP.
- repositories abstrae persistencia (fácil de cambiar SQLite → Postgres).

## 4. Reglas para la API REST

- Prefijo /api/v1
- Versionado por path
- Verbos HTTP semánticos: GET/POST/DELETE (PUT sólo para replace completo, PATCH para parcial)
- Paginación: ?limit=20&offset=0
- Errores con formato RFC 7807 (application/problem+json)
- Códigos: 200, 201, 204, 400, 404, 409, 422, 500
- Streaming de audio vía StreamingResponse con media_type="audio/wav" o "audio/mpeg"
- IDs públicos en UUIDv4 (no exponer PK de BD)

## 5. Manejo de errores

- Toda excepción de dominio hereda de OmniVoiceAPIError (en core/exceptions.py)
- Conversión centralizada en un handler de FastAPI
- Nunca filtrar trazas al cliente; log interno con structlog

## 6. Configuración

- Toda config via Settings (pydantic-settings) cargada desde .env
- Nunca hardcoded secrets
- Validar al arranque (VRAM mínima, paths existentes, modelo descargado)

## 7. Async / sync

- Endpoints async def
- Engines síncronos → envolver con `asyncio.to_thread`
- EnginePool con `asyncio.Semaphore(2)` (concurrencia controlada)
- Pocket TTS funciona en CPU; EdgeTTS es cloud; OmniVoice necesita GPU

## 8. Testing

- Cobertura mínima: 80% en src/
- Tests unitarios: lógica de servicios con engine mockeado
- Tests de integración: API end-to-end con fixtures de audio
- Fixtures en tests/conftest.py
- Patrón AAA (Arrange, Act, Assert)
- Nombres: test_<unit>_<scenario>_<expected>
- Mock del engine vía `TtsEngineBase` ABC

## 9. Git / Commits

- Conventional Commits: feat:, fix:, docs:, test:, refactor:, chore:, perf:
- Una feature = un commit atómico (o squash semántico)
- Branches: feat/<sprint>-<slug>, fix/<slug>, docs/<slug>
- PRs pequeños (<400 líneas diff)

## 10. Documentación

- Cada endpoint con summary, description, response_model, responses
- Ejemplos en OpenAPI vía examples= de Pydantic
- README siempre actualizado con quickstart
- Diagramas en docs/ (Mermaid)

## 11. Convenciones de audio

- Sample rate de referencia:
  - **24000 Hz** — Pocket TTS, EdgeTTS (y cuando se clona para ambos engines)
  - **22050 Hz** — OmniVoice (se resamplea de 24000→22050 en tiempo de síntesis)
- Formato de salida: WAV (PCM 16-bit, mono)
- Resample y time-stretch (`speed`): **ffmpeg** (`aresample` / `atempo` vía
  `omnivoice_api/core/ffmpeg.py`). Devuelven `None` si falta el binario y cada
  llamante lanza su error de dominio
- `librosa` está **prohibido**: arrastra `numba`, que fija o downgradea `numpy`
  y rompe el stack `torch 2.5.1` + `pocket-tts` (el gateway actual corre sin
  `librosa`). Quien necesite DSP use `ffmpeg` o `numpy`/`scipy`, nunca librosa
- Conversión a MP3 opcional vía lameenc
- Audio de referencia para clonado: 5-30 s, mono, sin ruido de fondo
- Engine de referencia determinado por `TTS_ENGINE` o parámetro `engines`

## 12. Reglas especiales para aider

- Antes de proponer cambios, leer siempre PHASES.md y ubicar la fase activa
- Nunca generar código sin tests asociados
- Cada prompt a aider debe referenciar: [Fase X] <tarea>
- Si una tarea excede 1 archivo → partir en subtareas
- Tras cada cambio: make test (o pytest -q) debe pasar en verde
- Mantén el fichero PhasesStatus.md marcando las fases completadas con [x]


