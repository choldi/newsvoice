# OmniVoice API - Especificación Simplificada

## Resumen

API REST para síntesis de voz (TTS) con soporte para voces stock, clonado zero-shot, voice design y conversaciones multi-voz.

**Base URL:** `/api/v1`  
**Formato:** JSON (requests) / audio/wav (responses de audio)  
**Autenticación:** Opcional `X-API-Key` header

---

## Endpoints

### 1. Salud del sistema

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/health` | Health check con estado del engine |
| GET | `/health/live` | Liveness probe |
| GET | `/health/ready` | Readiness check (modelo, instalación, venv) |

**GET `/health` — Response 200:**
```json
{
  "status": "ok",
  "version": "0.1.0",
  "device": "cuda:0",
  "mode": "REAL",
  "real_engine_error": null
}
```

`status` es `"ok"` cuando el modelo está cargado, `"degraded"` si está en modo mock o sin modelo.

**GET `/health/live` — Response 200:**
```json
{ "status": "alive" }
```

**GET `/health/ready` — Response 200/503:**
```json
{
  "status": "ready",
  "checks": {
    "install_dir_exists": true,
    "venv_python_exists": true,
    "model_loaded": true
  }
}
```

---

### 2. Voces Stock (predefinidas)

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/voices/stock` | Listar voces stock disponibles |

**Query params:**
- `language` (opcional): Filtrar por ISO 639-1 (ej: `es`, `en`)

**Response 200:**
```json
[
  {
    "voice_id": "es-mx-male",
    "language": "es",
    "gender": "male",
    "name": "Spanish MX Male"
  },
  {
    "voice_id": "en-us-female",
    "language": "en",
    "gender": "female",
    "name": "English US Female"
  }
]
```

---

### 3. Síntesis de Voz (TTS)

| Método | Ruta | Descripción |
|--------|------|-------------|
| POST | `/tts` | Generar audio desde texto (voz stock o instruct) |

**Query params:**
- `stream` (bool, default `false`): Streaming por chunks

**Request Body:**
```json
{
  "text": "Hola, ¿cómo estás?",
  "voice_id": "es-mx-male",
  "language": "es",
  "speed": 1.0,
  "instruct": null,
  "emotion": null,
  "num_step": 32,
  "denoise": true,
  "guidance_scale": 2.0,
  "duration": null,
  "preprocess_prompt": true,
  "postprocess_output": true,
  "pad_duration": 0.1,
  "fade_duration": 0.1,
  "audio_chunk_duration": 15.0,
  "audio_chunk_threshold": 30.0
}
```

**Campos:**
| Campo | Tipo | Requerido | Default | Descripción |
|-------|------|-----------|---------|-------------|
| `text` | string | Sí | - | Texto a sintetizar |
| `voice_id` | string | Sí | - | ID de voz (stock o clonada) |
| `language` | string | Sí | - | ISO 639-1 |
| `speed` | float | No | 1.0 | Velocidad (0.5 - 2.0) |
| `instruct` | string \| null | No | null | Instruct de voice design libre (ej: `"female, young adult, whisper"`) |
| `emotion` | string \| null | No | null | Emoción a aplicar: `happy`, `sad`, `angry`, `excited`, `calm`, `nervous`, `whisper`, `singing` |
| `num_step` | int | No | 32 | Pasos de unmasking (1-100, mayor = mejor calidad) |
| `denoise` | bool | No | true | Aplicar denoise para voz más limpia |
| `guidance_scale` | float | No | 2.0 | Classifier-free guidance (0.0-10.0) |
| `duration` | float \| null | No | null | Duración fija en segundos (0.5-120.0, sobrescribe speed) |
| `preprocess_prompt` | bool | No | true | Preprocesar audio de referencia |
| `postprocess_output` | bool | No | true | Eliminar silencios largos del output |
| `pad_duration` | float | No | 0.1 | Silencio por lado en segundos (0.0-1.0) |
| `fade_duration` | float | No | 0.1 | Duración fade-in/out en segundos (0.0-1.0) |
| `audio_chunk_duration` | float | No | 15.0 | Duración target por chunk (1.0-60.0s) |
| `audio_chunk_threshold` | float | No | 30.0 | Umbral para activar chunking (5.0-120.0s) |

**Response 200:** `audio/wav`

**Errores:**
- 400: Texto vacío, instruct inválido (token no soportado)
- 404: Voz no encontrada
- 400: Idioma no soportado
- 503: Motor no disponible

---

### 4. Voice Design (instruct personalizado)

| Método | Ruta | Descripción |
|--------|------|-------------|
| POST | `/tts/instruct` | Sintetizar con instruct de voice design libre |
| GET | `/tts/voice-design/tokens` | Listar tokens válidos para instructs |

#### POST `/tts/instruct`

Voice design libre: especifica atributos del hablante directamente mediante un instruct. No requiere una voz existente.

**Query params:**
- `stream` (bool, default `false`): Streaming por chunks

**Request Body:**
```json
{
  "text": "Hello, how are you?",
  "instruct": "female, young adult, british accent",
  "language": "en",
  "speed": 1.0,
  "emotion": null,
  "num_step": 32,
  "denoise": true,
  "guidance_scale": 2.0,
  "duration": null,
  "preprocess_prompt": true,
  "postprocess_output": true,
  "pad_duration": 0.1,
  "fade_duration": 0.1,
  "audio_chunk_duration": 15.0,
  "audio_chunk_threshold": 30.0
}
```

**Campos:**
| Campo | Tipo | Requerido | Default | Descripción |
|-------|------|-----------|---------|-------------|
| `text` | string | Sí | - | Texto a sintetizar |
| `instruct` | string | Sí | - | Instruct de voice design |
| `language` | string | Sí | - | ISO 639-1 |
| `speed` | float | No | 1.0 | Velocidad (0.5 - 2.0) |
| `emotion` | string \| null | No | null | Emoción a aplicar (ver emociones soportadas) |
| `num_step` - `audio_chunk_threshold` | - | No | - | Mismos parámetros que POST /tts |

**Response 200:** `audio/wav`

**Errores:**
- 400: Instruct con tokens no soportados (ver tokens válidos abajo)
- 400: Emoción no soportada
- 400: Idioma no soportado
- 503: Motor no disponible

#### GET `/tts/voice-design/tokens`

Devuelve los tokens de instruct agrupados por categoría.

**Response 200:**
```json
{
  "gender": ["male", "female"],
  "age": ["child", "teenager", "young adult", "middle-aged", "elderly"],
  "pitch": ["very low pitch", "low pitch", "moderate pitch", "high pitch", "very high pitch"],
  "style": ["whisper"],
  "english_accent": [
    "american accent", "australian accent", "british accent", "canadian accent",
    "chinese accent", "indian accent", "japanese accent", "korean accent",
    "portuguese accent", "russian accent"
  ],
  "chinese_dialect": [
    "河南话", "陕西话", "四川话", "贵州话", "云南话", "桂林话",
    "济南话", "石家庄话", "甘肃话", "宁夏话", "青岛话", "东北话"
  ]
}
```

---

### 5. Emociones Soportadas

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/emotions` | Listar emociones disponibles |

Las emociones se aplican como **tags de texto** (prefijos) en el contenido a sintetizar. El modelo `ModelsLab/omnivoice-singing` las interpreta directamente.

**Response 200:**
```json
[
  { "id": "happy", "name": "Happy", "description": "Tono alegre y contento" },
  { "id": "sad", "name": "Sad", "description": "Tono melancólico o triste" },
  { "id": "angry", "name": "Angry", "description": "Tono enfadado o iracundo" },
  { "id": "excited", "name": "Excited", "description": "Tono entusiasmado y enérgico" },
  { "id": "calm", "name": "Calm", "description": "Tono sereno y relajado" },
  { "id": "nervous", "name": "Nervous", "description": "Tono tenso o ansioso" },
  { "id": "whisper", "name": "Whisper", "description": "Voz susurrada" },
  { "id": "singing", "name": "Singing", "description": "Estilo cantado / melódico" }
]
```

**Uso:** Envía el parámetro `emotion` en POST `/tts` o POST `/tts/instruct`, o incluye el tag directamente en el texto: `"[happy] ¡Qué alegría verte!"`.

---

### 6. Voces Clonadas (Zero-shot)

| Método | Ruta | Descripción |
|--------|------|-------------|
| POST | `/voices/clone` | Crear voz clonada desde audio de referencia |
| GET | `/voices/cloned` | Listar voces clonadas (paginado) |
| GET | `/voices/cloned/{voice_id}` | Obtener detalles de voz clonada |
| DELETE | `/voices/cloned/{voice_id}` | Eliminar voz clonada |

#### POST `/voices/clone`

**Content-Type:** `multipart/form-data`

| Campo | Tipo | Requerido | Descripción |
|-------|------|-----------|-------------|
| `reference_audio` | file | Sí | Audio WAV/FLAC (5-30s, mono, 22050Hz) |
| `name` | string | Sí | Nombre único (3-64 chars, `^[a-zA-Z0-9_-]+$`) |
| `language` | string | Sí | ISO 639-1 |

**Response 201:**
```json
{
  "voice_id": "uuid-v4",
  "name": "mi-voz",
  "language": "es",
  "message": "Voz 'mi-voz' clonada exitosamente"
}
```

**Errores:**
- 409: Nombre de voz duplicado
- 422: Idioma no soportado o audio de referencia inválido

#### GET `/voices/cloned`

**Query params:**
- `language` (opcional): Filtrar por ISO 639-1
- `limit` (default 100, max 1000)
- `offset` (default 0)

**Response 200:** `array` de voces clonadas

#### GET `/voices/cloned/{voice_id}`

**Response 200:** Objeto con detalles de la voz clonada  
**Response 404:** Voz no encontrada

#### DELETE `/voices/cloned/{voice_id}`

**Response 204:** Eliminada  
**Response 404:** Voz no encontrada

---

### 7. Conversaciones Multi-voz

| Método | Ruta | Descripción |
|--------|------|-------------|
| POST | `/conversations` | Generar diálogo entre múltiples voces |

**Query params:**
- `stream` (bool, default `false`): Streaming por chunks

**Request Body:**
```json
{
  "turns": [
    { "voice_id": "es-mx-male", "text": "Hola, ¿cómo estás?" },
    { "voice_id": "en-us-female", "text": "I'm doing great, thanks!" }
  ],
  "pause_ms": 300
}
```

**Campos:**
| Campo | Tipo | Requerido | Default | Descripción |
|-------|------|-----------|---------|-------------|
| `turns` | array | Sí | - | Mínimo 2 turnos |
| `turns[].voice_id` | string | Sí | - | Voz stock |
| `turns[].text` | string | Sí | - | Texto del turno |
| `pause_ms` | int | No | 300 | Silencio entre turnos (0-5000 ms) |

**Response 200:** `audio/wav` concatenado con silencios

**Errores:**
- 400: Menos de 2 turnos, texto vacío
- 404: Alguna voz no existe
- 503: Motor no disponible

---

## Códigos de Error

Todos los errores devuelven JSON con `detail` y `error_type`:

```json
{
  "detail": "Voz no encontrada: abc (stock)",
  "error_type": "voice_not_found",
  "voice_id": "abc",
  "valid_voice_ids": ["es-mx-male", "en-us-male"]
}
```

| Código | `error_type` | Cuándo |
|--------|--------------|--------|
| 400 | `unsupported_instruct` | Token de instruct no soportado |
| 400 | `unsupported_language` | Idioma no soportado |
| 400 | `validation_error` | Parámetros inválidos (texto vacío, turnos insuficientes) |
| 404 | `voice_not_found` | Voz stock o clonada no existe |
| 409 | - | Nombre de voz duplicado al clonar |
| 422 | - | Audio de referencia inválido |
| 503 | `engine_unavailable` | Motor ocupado / VRAM OOM |
| 500 | `internal_error` | Error inesperado |

---

## Límites y Rendimiento

| Límite | Valor |
|--------|-------|
| Audio referencia (clonado) | 5-30 segundos |
| Tamaño upload máximo | 10 MB |
| Concurrencia motor (P2000) | 1 síntesis simultánea |
| TTL outputs temporales | 1 hora |
| Rate limiting (opcional) | 60 req/min por API key |

---

## Ejemplos de Uso

### cURL - TTS con voz stock
```bash
curl -X POST "http://localhost:8000/api/v1/tts" \
  -H "Content-Type: application/json" \
  -d '{"text": "Hola mundo", "voice_id": "es-mx-male", "language": "es"}' \
  --output output.wav
```

### cURL - TTS con voice design (instruct)
```bash
curl -X POST "http://localhost:8000/api/v1/tts" \
  -H "Content-Type: application/json" \
  -d '{"text": "Hola mundo", "voice_id": "es-mx-male", "language": "es", "instruct": "female, whisper"}' \
  --output output.wav
```

### cURL - TTS con emoción
```bash
curl -X POST "http://localhost:8000/api/v1/tts" \
  -H "Content-Type: application/json" \
  -d '{"text": "¡Qué alegría verte hoy!", "voice_id": "es-mx-male", "language": "es", "emotion": "happy"}' \
  --output happy.wav
```

### cURL - Emoción + instruct combinados
```bash
curl -X POST "http://localhost:8000/api/v1/tts" \
  -H "Content-Type: application/json" \
  -d '{"text": "Esta es una noticia terrible", "voice_id": "en-us-female", "language": "en", "emotion": "sad", "instruct": "female, young adult, british accent"}' \
  --output sad.wav
```

### cURL - Voice design libre (sin voz stock)
```bash
curl -X POST "http://localhost:8000/api/v1/tts/instruct" \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello world", "instruct": "male, young adult, british accent", "language": "en"}' \
  --output output.wav
```

### cURL - Clonar voz
```bash
curl -X POST "http://localhost:8000/api/v1/voices/clone" \
  -F "reference_audio=@reference.wav" \
  -F "name=mi-voz" \
  -F "language=es"
```

### cURL - Conversación
```bash
curl -X POST "http://localhost:8000/api/v1/conversations" \
  -H "Content-Type: application/json" \
  -d '{
    "turns": [
      {"voice_id": "es-mx-male", "text": "Hola"},
      {"voice_id": "en-us-female", "text": "Hello, how are you?"}
    ],
    "pause_ms": 500
  }' \
  --output dialogue.wav
```

### Python - Cliente simple
```python
import httpx


async def tts(text: str, voice_id: str, language: str) -> bytes:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "http://localhost:8000/api/v1/tts",
            json={"text": text, "voice_id": voice_id, "language": language},
        )
        resp.raise_for_status()
        return resp.content
```

---

## Versionado

- Versión actual: `v1`
- Prefijo obligatorio: `/api/v1`
- Cambios breaking → nueva versión `/api/v2`
