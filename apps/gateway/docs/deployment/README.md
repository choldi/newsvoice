# Guía de Despliegue — TTS API (Multi-Engine)

> **EN:** Deployment guide for the multi-engine TTS API. GPU is optional (only needed for OmniVoice engine).

## Requisitos previos

- Python 3.11+
- uv (package manager) — se instala automáticamente con `make check-uv`
- **GPU NVIDIA con CUDA** — solo para `TTS_ENGINE=omnivoice`
- **Internet** — para `TTS_ENGINE=edgetts` y la descarga inicial de Pocket TTS, Kokoro y voces Piper

## Instalación por engine

```bash
# Engine recomendado para CPU (clonado de voz)
make install ENGINE=pocket_tts

# Engine cloud (sin GPU, incluye catalán)
make install ENGINE=edgetts

# Engine local CPU con voces ONNX (incluye catalán)
make install ENGINE=piper

# Engine GPU (full features: emociones, voice design)
make install ENGINE=omnivoice

# Todos los engines
make install-all
```

## Configuración (.env)

```bash
cp .env.example .env
```

### Variables de engine

| Variable | Descripción | Default |
|----------|-------------|---------|
| `TTS_ENGINE` | Engine activo: `pocket_tts`, `piper`, `edgetts`, `omnivoice`, `mock`, `routed` | `omnivoice` |
| `TTS_ENGINES` | JSON routing por idioma (cuando `routed`) | `""` |
| `POCKET_TTS_DEFAULT_LANGUAGE` | Idioma por defecto (ISO 639-1) que se carga al arrancar | `es` |
| `POCKET_TTS_MODEL` | Override global del checkpoint (config YAML) | `""` |
| `POCKET_TTS_REFERENCES` | JSON de WAVs de referencia para voces stock ES/FR | `""` |
| `EDGETTS_VOICE_PREFIX` | Locale EdgeTTS | `es-MX` |
| `PIPER_VOICE` | Modelo Piper por defecto | `ca_ES-upc_ona-medium` |
| `PIPER_VOICES_DIR` | Directorio de modelos `.onnx` | `storage/piper_voices` |
| `PIPER_AUTO_DOWNLOAD` | Descargar la voz si falta | `true` |
| `PIPER_USE_CUDA` | Usar CUDA en lugar de CPU | `false` |
| `AUTO_TRANSCRIBE` | Auto-transcribir audio de referencia | `false` |

### Variables comunes

| Variable | Descripción | Default |
|----------|-------------|---------|
| `API_KEY` | API key opcional (vacío = sin auth) | `""` |
| `CORS_ORIGINS` | Orígenes CORS permitidos | `*` |
| `DATABASE_URL` | URL de SQLite para voces clonadas | `sqlite:///storage/omnivoice.db` |
| `LOG_LEVEL` | Nivel de logging | `INFO` |

### Variables OmniVoice (solo `TTS_ENGINE=omnivoice`)

| Variable | Descripción | Default |
|----------|-------------|---------|
| `OMNIVOICE_MODEL_ID` | Modelo HuggingFace | `ModelsLab/omnivoice-singing` |
| `OMNIVOICE_DEVICE` | Dispositivo de inferencia | `cuda:0` |
| `OMNIVOICE_FALLBACK_TO_MOCK` | Fallback a mock si falla | `true` |

### Voces Piper (solo `TTS_ENGINE=piper`)

El gateway descarga el `.onnx` a `PIPER_VOICES_DIR` la primera vez que carga
una voz si `PIPER_AUTO_DOWNLOAD=true`. Usa `piper.download_voices` cuando está
disponible y mantiene el antiguo `piper.download` como respaldo; si no aparece
el `.onnx`, la petición falla con `EngineUnavailableError`.

Para precargar la voz o trabajar sin Internet:

```bash
python -m piper.download_voices ca_ES-upc_ona-medium
```

## Despliegue con Docker

```bash
# Construir
docker build -t tts-api .

# Ejecutar con Pocket TTS (sin GPU)
docker run -d \
  -p 8000:8000 \
  -e TTS_ENGINE=pocket_tts \
  -e API_KEY=tu-api-key \
  -v ./storage:/app/storage \
  --name tts-api \
  tts-api

# Ejecutar con OmniVoice (con GPU)
docker run -d \
  --gpus all \
  -p 8000:8000 \
  -e TTS_ENGINE=omnivoice \
  -e OMNIVOICE_PATH=/path/to/omnivoice \
  -v ./storage:/app/storage \
  --name tts-api \
  tts-api
```

## Despliegue con routed engine (recomendado para podcasts)

Enrutamiento por idioma: es/en/fr → Pocket TTS (CPU, clonado), ca → EdgeTTS (cloud):

```bash
# 1. Instalar todos los engines
make install-all

# 2. Configurar en .env
cat >> .env << EOF
TTS_ENGINE=routed
TTS_ENGINES={"es":"pocket_tts","en":"pocket_tts","fr":"pocket_tts","ca":"edgetts","_default":"pocket_tts"}
EOF

# 3. Iniciar
make run
```

## Despliegue sin Docker (Linux)

### Instalar

```bash
pip install uv
make install ENGINE=pocket_tts
```

### Ejecutar con systemd

Crear `/etc/systemd/system/tts-api.service`:

```ini
[Unit]
Description=TTS API (Multi-Engine)
After=network.target

[Service]
Type=simple
user=tts-api
workingdirectory=/opt/tts-api
Environment=TTS_ENGINE=pocket_tts
ExecStart=/opt/tts-api/.venv/bin/uvicorn omnivoice_api.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable tts-api
sudo systemctl start tts-api
```

## Verificación post-despliegue

```bash
# Health check (muestra engine activo)
curl http://localhost:8000/api/v1/health
# → {"status":"ok", "engine":"routed(edgetts, pocket_tts)", ...}

# Liveness
curl http://localhost:8000/api/v1/health/live

# Listar voces stock
curl http://localhost:8000/api/v1/voices/stock

# Probar TTS en español (Pocket TTS)
curl -X POST http://localhost:8000/api/v1/tts \
  -H "Content-Type: application/json" \
  -d '{"text":"Hola mundo","voice_id":"es-mx-female","language":"es"}' \
  --output test_es.wav

# Probar TTS en catalán (EdgeTTS)
curl -X POST http://localhost:8000/api/v1/tts \
  -H "Content-Type: application/json" \
  -d '{"text":"Bon dia","voice_id":"ca-female","language":"ca"}' \
  --output test_ca.wav

# Clonar voz para ambos engines
curl -X POST http://localhost:8000/api/v1/voices/clone \
  -F "name=mi_voz" -F "language=es" \
  -F "reference_audio=@voz.wav" \
  -F "engines=pocket_tts,omnivoice"
```

## Registro declarativo de engines

El catálogo se versiona en `config/engines.toml` y se copia a
`/app/config/engines.toml` en la imagen. `deploy/docker-compose.yml` monta el
directorio `config/` del repo en `/app/config` para que la Admin API pueda
escribir `engines.local.json` en el host.

| Variable | Descripción | Default |
|----------|-------------|---------|
| `TTS_ENGINE_REGISTRY_PATH` | Ruta del manifiesto TOML | `config/engines.toml` |
| `TTS_ENGINE_OVERRIDE_PATH` | Ruta del override JSON | `config/engines.local.json` |

`TTS_ENGINE` sigue siendo el valor por defecto cuando no hay un engine activo
preparado. La Admin API valida y escribe el override; devuelve
`restart_required: true` y el cambio se aplica al recrear/reiniciar el gateway.
No escribas secretos en el override: solo nombres de variables de entorno.

Para añadir un proveedor OpenAI-compatible sin código nuevo:

```bash
curl -X PUT "http://localhost:8001/api/v1/admin/engines/vendor" \
  -H "X-API-Key: $GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "kind": "openai_compatible_http",
    "display_name": "Vendor TTS",
    "locations": ["cloud"],
    "capabilities": {"supported_languages": ["es"]},
    "options": {
      "base_url_env": "VENDOR_BASE_URL",
      "api_key_env": "VENDOR_API_KEY",
      "model": "vendor-model",
      "voices": {"vendor-es": {"voice": "es-ES-Voice", "language": "es"}}
    }
  }'

curl -X POST "http://localhost:8001/api/v1/admin/engines/activate" \
  -H "X-API-Key: $GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"id":"vendor"}'
```

Después configura `VENDOR_BASE_URL` y `VENDOR_API_KEY` en
`apps/gateway/.env`/compose y reinicia el gateway.

## Monitoreo

- **Métricas**: `GET /metrics` (Prometheus)
- **Docs**: `GET /docs` (Swagger UI)
- **OpenAPI**: `GET /openapi.json`

---

**EN:** Install with `make install ENGINE=<engine>`. Set `TTS_ENGINE` in `.env`. For language-based routing (e.g., Spanish→PocketTTS, Catalan→EdgeTTS), use `TTS_ENGINE=routed` with `TTS_ENGINES` JSON mapping.
