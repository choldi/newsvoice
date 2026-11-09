# TTS API — Multi-Engine

> **EN:** REST API for multilingual TTS with **multiple engine backends**: Pocket TTS, EdgeTTS, Kokoro, OmniVoice, Qwen3-TTS, Fish Audio and any OpenAI-compatible `/audio/speech` provider. Selectable via `TTS_ENGINE` with language-based routing.

API REST para síntesis de voz (TTS) multilingüe con **múltiples engines**:
- **Pocket TTS** — CPU, clonado de voz, es/en/fr
- **EdgeTTS** — Cloud, 400+ voces (incluye catalán ca-ES), sin clonado
- **Kokoro** — CPU, voces stock es/en/fr/it/pt/hi, sin catalán ni clonado
- **OmniVoice** — GPU, emociones, voice design, instrucciones
- **OmniVoice remoto** — proxy HTTP al sidecar `gateway-omnivoice`
- **Qwen3-TTS** — GPU, clonado y voice design, local o sidecar `gateway-qwen3`
- **Fish Audio** — cloud, reference ids y clones inline, sin voice design
- **OpenAI-compatible** — cloud, `/audio/speech`, solo voces stock

> Ver [`docs/Architecture.md`](docs/Architecture.md) para detalles de arquitectura.

## Características

- 🔀 **Multi-engine** — `TTS_ENGINE=routed|pocket_tts|edgetts|kokoro|qwen3_tts|omnivoice|omnivoice_remote|fish_tts|openai_tts|mock`
- 🌐 **Enrutado por idioma** — `TTS_ENGINE=routed` distribuye es/en/fr → Pocket TTS, ca → EdgeTTS
- ☁️ **Cloud opt-in** — Fish Audio y OpenAI-compatible se activan con sus credenciales y nunca degradan el health global
- 🎙️ **TTS multilingüe** con voces stock predefinidas
- 🔄 **Clonado de voz** multi-engine (`engines=pocket_tts,omnivoice`)
- 🏷️ **Etiquetado por engine** — cada voz clonada sabe para qué engines sirve
- 🎭 **Emociones y singing** (solo OmniVoice): `happy`, `sad`, `angry`, `excited`, `calm`, `nervous`, `whisper`, `singing`
- 🎨 **Voice design libre** con tokens de atributos (solo OmniVoice)
- 💬 **Conversaciones multi-voz** con turnos y pausas configurables
- 📝 **Auto-transcripción** de audio de referencia (opcional, `faster-whisper`)
- 📦 **Persistencia SQLite** para metadatos de voces clonadas
- 📊 **Observabilidad** con logs estructurados y métricas Prometheus

## Pre-requisitos

- **Python 3.11+**
- **FFmpeg** (binario del sistema): resample del audio de referencia, `speed`
  (`atempo`) y conversión con pydub. Es el único backend de audio nativo.
  No instales `librosa` en este proyecto: su dependencia `numba` fija o
  downgradea `numpy` y rompe `torch 2.5.1` + `pocket-tts` (ver
  `docs/CONVENTIONS.md`, secciones 1 y 11).
- **GPU NVIDIA con CUDA** — necesario para `omnivoice`, `qwen3_tts` y `chatterbox`; los sidecars `gateway-omnivoice` / `gateway-qwen3` aíslan sus dependencias
- **Internet** — `edgetts`, Fish Audio y OpenAI-compatible; también la primera descarga de modelos locales

## Instalación rápida

```bash
# Clonar repositorio
git clone <repo-url>
cd ov_api

# Instalar con un engine específico
make install ENGINE=pocket_tts    # CPU, clonado de voz (recomendado)
make install ENGINE=edgetts       # Cloud, sin GPU
make install ENGINE=omnivoice     # GPU, full features
make install ENGINE=all           # Todos los engines

# Iniciar en modo desarrollo
TTS_ENGINE=pocket_tts make dev
```

La API estará disponible en:
- **API**: http://localhost:8000/api/v1
- **Docs (Swagger)**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc
- **Health**: http://localhost:8000/api/v1/health

## Configuración

Copia `.env.example` a `.env` y ajusta:

```bash
cp .env.example .env
```

### Variables principales

| Variable | Descripción | Default |
|----------|-------------|---------|
| `TTS_ENGINE` | Engine activo: `routed`, `pocket_tts`, `edgetts`, `piper`, `chatterbox`, `kokoro`, `qwen3_tts`, `omnivoice`, `omnivoice_remote`, `fish_tts`, `openai_tts`, `mock` | `omnivoice` |
| `TTS_ENGINES` | JSON de enrutado por idioma (cuando `TTS_ENGINE=routed`) | `""` |
| `TTS_EXTRA_ENGINES` | Engines instanciados sin rutear idioma, para pins `engine=...` | `""` |
| `QWEN3_TTS_REMOTE_URL` | URL del sidecar `gateway-qwen3`; vacío = `qwen3_tts` no disponible | `""` |
| `OMNIVOICE_REMOTE_URL` | URL del sidecar `gateway-omnivoice`; vacío mantiene el proxy inactivo | `http://gateway-omnivoice:8000` |
| `FISH_API_KEY` | Clave de Fish Audio; vacío desactiva el engine | `""` |
| `OPENAI_TTS_BASE_URL` | Base OpenAI-compatible con `/v1`; vacío desactiva el engine | `""` |
| `POCKET_TTS_DEFAULT_LANGUAGE` | Idioma por defecto (ISO 639-1) que se carga al arrancar | `es` |
| `POCKET_TTS_MODEL` | Override global del checkpoint (config YAML/ruta/URL) | `""` |
| `POCKET_TTS_REFERENCES` | JSON de WAVs de referencia para voces stock ES/FR clonadas | `""` |
| `EDGETTS_VOICE_PREFIX` | Locale por defecto EdgeTTS | `es-MX` |
| `KOKORO_REPO_ID` | Repo Hugging Face del modelo Kokoro | `hexgrad/Kokoro-82M` |
| `KOKORO_DEFAULT_LANGUAGE` | Idioma por defecto de Kokoro (sin catalán) | `es` |
| `KOKORO_VOICES` | JSON voz_id del gateway -> voz nativa de Kokoro | `""` |
| `AUTO_TRANSCRIBE` | Auto-transcribir audio de referencia | `false` |
| `DATABASE_URL` | URL de SQLite | `sqlite:///storage/omnivoice.db` |
| `API_KEY` | API Key opcional | `""` |
| `LOG_LEVEL` | Nivel de logging | `INFO` |

### Enrutado multi-engine (routed)

```env
TTS_ENGINE=routed
TTS_ENGINES={"es":"pocket_tts","en":"pocket_tts","fr":"pocket_tts","ca":"edgetts","_default":"pocket_tts"}
# Opcional: engines GPU/cloud que no rutean ningún idioma
TTS_EXTRA_ENGINES=kokoro,qwen3_tts,omnivoice_remote
```

Los engines cloud son opt-in: `fish_tts` necesita `FISH_API_KEY` y
`openai_tts` necesita `OPENAI_TTS_BASE_URL` (más `OPENAI_TTS_API_KEY` si el
proveedor lo exige). También puedes fijarlos por voz con
`TTS_ENGINE=fish_tts` / `openai_tts`, o añadirlos a `TTS_EXTRA_ENGINES`.

## Endpoints principales

### Health
- `GET /api/v1/health` — Estado general + engine activo
- `GET /api/v1/health/live` — Liveness probe
- `GET /api/v1/health/ready` — Readiness probe

### TTS
- `POST /api/v1/tts` — Síntesis con voz stock/clonada
- `POST /api/v1/tts/instruct` — Síntesis con instruct (solo OmniVoice)
- `GET /api/v1/tts/voice-design/tokens` — Tokens de voice design

### Voces
- `GET /api/v1/voices/stock` — Lista voces stock
- `POST /api/v1/voices/clone` — Clonar voz (`engines=pocket_tts,omnivoice`)
- `GET /api/v1/voices/cloned` — Listar voces clonadas (`?engine=pocket_tts`)
- `GET /api/v1/voices/cloned/{id}` — Detalle voz clonada
- `DELETE /api/v1/voices/cloned/{id}` — Eliminar voz clonada

### Conversaciones y Emociones
- `POST /api/v1/conversations` — Diálogo multi-voz
- `GET /api/v1/emotions` — Emociones soportadas

## Ejemplos de uso

### TTS con voz stock (Español)
```bash
curl -X POST "http://localhost:8000/api/v1/tts" \
  -H "Content-Type: application/json" \
  -d '{"text": "Hola, esto es una prueba.", "voice_id": "es-mx-female", "language": "es"}' \
  --output output.wav
```

### TTS en catalán (EdgeTTS)
```bash
curl -X POST "http://localhost:8000/api/v1/tts" \
  -H "Content-Type: application/json" \
  -d '{"text": "Bon dia, això és una prova.", "voice_id": "ca-female", "language": "ca"}' \
  --output catala.wav
```

### Clonar voz para múltiples engines
```bash
curl -X POST "http://localhost:8000/api/v1/voices/clone" \
  -F "name=mi_voz" \
  -F "language=es" \
  -F "reference_audio=@reference.wav" \
  -F "engines=pocket_tts,omnivoice" \
  -F "ref_text=Transcripción del audio de referencia"
```

### Sintetizar con voz clonada
```bash
curl -X POST "http://localhost:8000/api/v1/tts" \
  -H "Content-Type: application/json" \
  -d '{"text": "Esta es mi voz clonada.", "voice_id": "<VOICE_ID>", "language": "es"}' \
  --output clonada.wav
```

### Listar voces por engine
```bash
curl "http://localhost:8000/api/v1/voices/cloned?engine=pocket_tts"
```

### TTS con emoción (solo OmniVoice)
```bash
curl -X POST "http://localhost:8000/api/v1/tts" \
  -H "Content-Type: application/json" \
  -d '{"text": "¡Qué alegría verte!", "voice_id": "es-mx-male", "language": "es", "emotion": "happy"}' \
  --output happy.wav
```

### Voice design con instruct (solo OmniVoice)
```bash
curl -X POST "http://localhost:8000/api/v1/tts/instruct" \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello, this is a custom voice.", "instruct": "female, young adult, british accent", "language": "en"}' \
  --output design.wav
```

### Conversación multi-voz
```bash
curl -X POST "http://localhost:8000/api/v1/conversations" \
  -H "Content-Type: application/json" \
  -d '{"turns": [{"voice_id": "voz_1", "text": "Hola"}, {"voice_id": "voz_2", "text": "¿Cómo estás?"}], "pause_ms": 300}' \
  --output dialogo.wav
```

## Capacidades por engine

| Feature | Pocket TTS | EdgeTTS | Kokoro | OmniVoice | Qwen3-TTS | Mock |
|---------|:----------:|:-------:|:------:|:---------:|:---------:|:----:|
| Voces stock | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| Clonado de voz | ✅ | ❌ | ❌ | ✅ | ✅ | ❌ |
| Voice design (instruct) | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ |
| Emociones | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ |
| Idiomas es/en/fr | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Catalán (ca) | ❌ | ✅ | ❌ | ❌ | ❌ | ✅ |
| GPU requerida | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ |
| Internet requerido | Solo 1ª vez | ✅ | Solo 1ª vez | ❌ | Solo 1ª vez | ❌ |

| Feature | Fish Audio | OpenAI-compatible |
|---------|:----------:|:-----------------:|
| Voces stock | ✅ (reference ids) | ✅ |
| Clonado de voz | ✅ (inline references) | ❌ |
| Voice design / emociones | ❌ | ❌ |
| Idiomas | Amplio (`FISH_LANGUAGES`) | `OPENAI_TTS_LANGUAGES` |
| GPU requerida | ❌ | ❌ |
| Internet requerido | ✅ | ✅ |

## Estructura del proyecto

```
ov_api/
├── tts_api/
│   ├── api/v1/             # Routers FastAPI
│   ├── core/
│   │   ├── engine_base.py       # ABC + EngineCapabilities
│   │   ├── engine_factory.py    # Factory (TTS_ENGINE → engine)
│   │   ├── engine_client.py     # Cliente con logging/validación
│   │   ├── engine_pool.py       # Semáforo de concurrencia
│   │   ├── engines/             # Implementaciones
│   │   │   ├── pocket_tts_engine.py
│   │   │   ├── edgetts_engine.py
│   │   │   ├── kokoro_engine.py
│   │   │   ├── piper_engine.py
│   │   │   ├── chatterbox_engine.py
│   │   │   ├── qwen3_tts_engine.py
│   │   │   ├── qwen3_remote_engine.py
│   │   │   ├── omnivoice_engine_adapter.py
│   │   │   ├── omnivoice_remote_engine.py
│   │   │   ├── fish_tts_engine.py
│   │   │   ├── openai_tts_engine.py
│   │   │   ├── mock_engine.py
│   │   │   └── routed_engine.py
│   │   ├── audio.py             # Validación de audio (24k/22k)
│   │   └── exceptions.py
│   ├── services/           # TtsService, VoiceService, ConversationService
│   ├── repositories/       # VoiceRepository (SQLite)
│   ├── settings.py         # Configuración
│   └── main.py             # Entry point
├── docs/
├── tests/
├── storage/
├── pyproject.toml
├── Makefile
└── README.md
```

## Desarrollo

```bash
make test              # Tests con cobertura
make lint              # ruff + mypy
make format            # Formateo automático
make dev               # Servidor desarrollo con reload
make install ENGINE=pocket_tts  # Instalar con engine específico
```

## Despliegue

```bash
# Instalar con Pocket TTS + EdgeTTS
make install-all

# Configurar enrutado en .env
echo 'TTS_ENGINE=routed' >> .env
echo 'TTS_ENGINES={"es":"pocket_tts","ca":"edgetts","_default":"pocket_tts"}' >> .env

# Iniciar
make run
```

En el monorepo, los engines pesados se despliegan como sidecars internos:

- `gateway-omnivoice` (`--profile omnivoice`) — OmniVoice real; el gateway lo
  consume con `OMNIVOICE_REMOTE_URL` y `omnivoice_remote`.
- `gateway-qwen3` (`--profile qwen3`, requiere overlay GPU) — torch + qwen-tts
  en su propia imagen; se consume con `QWEN3_TTS_REMOTE_URL` y el nombre
  público sigue siendo `qwen3_tts`.

Fish Audio y OpenAI-compatible no necesitan sidecar: se configuran con sus
variables en `.env` y se activan como extra o por pin de voz.

Ver `docs/deployment/` para guías con Docker y Nginx/Caddy.

## Licencia

MIT License — ver `LICENSE` para detalles.

---

**EN:** See [English quick reference](#english-quick-reference) below the structure section. For full English docs, see `docs/api_spec_simplified.md`.
