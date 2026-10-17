# Arquitectura de TTS API

## 1. Visión general

Servicio HTTP que expone las capacidades de ModelsLab/omnivoice-singing (finetune de k2-fsa/OmniVoice) —TTS multilingüe, clonado zero-shot, voice design, emociones y singing— como API REST versionada, con persistencia de voces clonadas y generación de conversaciones multi-voz.

```mermaid
flowchart LR
    Client -->|HTTPS| FastAPI
    FastAPI --> Router
    Router --> Services
    Services --> Repos
    Services --> EngineWrapper
    Repos --> SQLite[(SQLite metadata)]
    Repos --> FS[(Filesystem<br/>voices/ outputs/)]
    EngineWrapper --> OmniVoice
    OmniVoice -->|CUDA| GPU[P2000 GPU]

## 2. Componentes

### 2.1 API Layer (api/v1/)
- routers FastAPI, sin lógica de negocio
- validación Pydantic de entrada/salida
- serialización a JSON o streaming binario

### 2.2 Services Layer (services/)
- TtsService: síntesis con voz stock o clonada
- VoiceService: alta/baja/listado de voces clonadas, validación de audio
- ConversationService: genera turnos de diálogo concatenando TTS

### 2.3 Repositories (repositories/)
- VoiceRepository: CRUD sobre cloned_voices (SQLite)
- Abstracción para sustituir almacenamiento sin tocar servicios

### 2.4 Core Engine (core/omnivoice_engine.py)
- Envoltorio único sobre la librería OmniVoice
- Implementa OmniVoiceEngineInterface (Protocol) para facilitar mocking
- Responsable de:
  - Cargar modelo una sola vez (singleton)
  - Sintetizar con voz stock {language, speaker_id}
  - Sintetizar con voz clonada {reference_audio_path, text}
  - Serializar WAV en memoria (BytesIO)

### 2.5 Storage
- storage/voices/<uuid>/{reference.wav, meta.json} — voces clonadas
- storage/outputs/<job_uuid>.wav — outputs temporales (TTL 1h)
- storage/cache/ — caché de embeddings de voces clonadas

### 2.6 Settings (settings.py)
- OMNIVOICE_MODEL_PATH
- OMNIVOICE_DEVICE=cuda:0
- OMNIVOICE_LANGUAGES (lista permitida)
- MAX_REFERENCE_DURATION_SEC=30
- ENGINE_CONCURRENCY=1 (P2000)
- OUTPUT_TTL_SECONDS=3600

## 3. Modelo de datos

CREATE TABLE cloned_voices (
  id           TEXT PRIMARY KEY,        -- UUIDv4
  name         TEXT NOT NULL UNIQUE,
  language     TEXT NOT NULL,           -- ISO 639-1
  reference_path TEXT NOT NULL,
  duration_sec REAL NOT NULL,
  created_at   TEXT NOT NULL,
  metadata     TEXT                     -- JSON extendido
);

## 4. Flujo de una petición TTS (con voz clonada)

```mermaid
sequenceDiagram
    participant C as Client
    participant A as API
    participant S as TtsService
    participant R as VoiceRepository
    participant E as Engine
    C->>A: POST /api/v1/tts {voice_id, text}
    A->>S: synthesize(dto)
    S->>R: get_voice(voice_id)
    R-->>S: voice(ref_path)
    S->>E: tts_clone(ref_path, text)
    E->>E: load embedding (cache)
    E->>E: model.generate()
    E-->>S: WAV bytes
    S-->>A: AudioResult(wav, duration)
    A-->>C: 200 audio/wav (stream)

## 5. Concurrencia y rendimiento

- P2000 = 5 GB VRAM → 1 síntesis simultánea
- asyncio.Semaphore(1) en EngineWrapper
- Endpoints async; el motor se llama con asyncio.to_thread
- Warm-up del modelo al arranque (síntesis de prueba)
- Caché LRU (128 voces) de embeddings calculados
- Para conversaciones largas: streaming por turnos

## 6. Errores y resiliencia

- EngineUnavailable, VoiceNotFound, InvalidReferenceAudio, UnsupportedLanguage, UnsupportedInstruct, VRAMOutError
- Handler global → application/problem+json
- Health endpoint /api/v1/health comprueba: GPU disponible, modelo cargado, DB accesible
- /api/v1/health/live vs /api/v1/health/ready

## 7. Seguridad

- CORS configurable
- API key en cabecera X-API-Key (opcional, gate por settings)
- Validación de tamaño de upload (max 10 MB)
- Sanitización de nombres de voz (regex ^[a-zA-Z0-9_-]{3,64}$)
- Outputs en subdirectorio sin ejecución

## 8. Observabilidad

- structlog con contexto por request (request_id)
- Métricas Prometheus-ready vía prometheus-fastapi-instrumentator
- Logs en JSON a stdout para recolección

## 9. Despliegue

- Local dev: uvicorn tts_api.main:app --reload
- Producción: uvicorn detrás de Nginx/Caddy en Windows Service o contenedor Docker Windows
- Bootstrap script: descarga modelo si no está, valida CUDA, crea DB

