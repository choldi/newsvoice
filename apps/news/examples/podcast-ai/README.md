# Podcast AI Pulse - Configuración de ejemplo

Este directorio contiene los archivos necesarios para configurar y ejecutar el podcast
diario de novedades de IA "AI Pulse" usando el news-engine.

## Contenido

| Archivo | Descripción |
|---------|-------------|
| `collections.ai.toml` | Configuración completa de la colección `ai` lista para copiar a `collections.toml` |
| `narration-spec.json` | Especificación de narración para dos hosts (Dwight + MichaelScott) |
| `narration-script.example.json` | Ejemplo de script de narración para render manual con `--script` |
| `check_feeds.py` | Verificador de operatividad de los 5 feeds RSS |

## Verificación de feeds (comprobada 2026-09-28)

Todos los feeds responden **HTTP 200** y devuelven items válidos:

```
TechCrunch AI  → 20 items  (https://techcrunch.com/category/artificial-intelligence/feed/)
Verge AI       → 10 items  (https://www.theverge.com/rss/ai-artificial-intelligence/index.xml)
Wired AI       → 10 items  (https://www.wired.com/feed/tag/ai/latest/rss)
MarkTechPost   → 10 items  (https://www.marktechpost.com/feed/)
arXiv stat.ML  → 59 items  (https://export.arxiv.org/rss/stat.ML)
Total:         → ~109 items por ejecución
```

## Uso

### 1. Copiar la configuración al servidor

```bash
# En el servidor de despliegue (ej. 192.168.5.7:/home/jordi/news-summary)
cp examples/podcast-ai/collections.ai.toml collections.toml
# O añadir el bloque [[collections]] al collections.toml existente
```

### 2. Registrar el schedule en Temporal

```bash
# Desde el contenedor worker
docker compose exec worker python -m src.workers.schedule --collection ai
```

### 3. Ejecución manual (para probar)

```bash
# Disparar el digest pipeline
docker compose exec worker python -m src.workers.trigger --collection ai

# Disparar solo la narración (requiere digest previo)
docker compose exec worker python -m src.workers.trigger_narration \
    --collection ai \
    --spec examples/podcast-ai/narration-spec.json \
    --script examples/podcast-ai/narration-script.example.json
```

### 4. Verificar feeds localmente

```bash
python examples/podcast-ai/check_feeds.py
```

## Voces usadas (clonadas en OmniVoice)

| Rol | Voz | UUID | Engine | Idioma |
|-----|-----|------|--------|--------|
| Michael (host entusiasta) | MichaelScott | `3e47fbcc-53a8-4551-be91-48f93d62a19f` | pocket_tts | en |
| Dwight (analista deadpan) | Dwight | `f0c280e3-0b2d-4f91-9ad6-ff0f86cf49f8` | pocket_tts | en |

> **Nota**: La voz se llama **Dwight** (no "Dwaigt"). Ambas están en inglés y usan PocketTTS.

## Configuración clave

- **Horario**: 11:00 UTC diario
- **Estructura**: 5 historias principales (extendidas, 5-6 frases) + 8 titulares
- **Idioma**: Inglés (`en`)
- **Estrategia narración**: `conversations` (una sola velocidad para todo el diálogo)
- **Bloques**: el diálogo se trocea en requests de ≤4000 caracteres (cortes en
  frontera de turno, nunca a mitad de frase) con 1 reintento por bloque
- **Música**: Ninguna
- **Entrega narración**: Habilitada (`narration_deliver_audio = true`)

## Variables de entorno relevantes en el servidor

```bash
NARRATION_ENABLED=true
NARRATION_API_BASE_URL=  # Empty = reuse TTS_API_BASE_URL (gateway, :8001). Never :8000
NARRATION_TASK_QUEUE=news-engine-narration-task-queue
NARRATION_FALLBACK_TO_EDGE=true  # General (single-voice) fallback stays enabled
NARRATIONS_DIALOG_FALLBACK_TO_EDGE=false  # Important: prevents silent substitution of cloned voices in dialogs
NARRATION_LANGUAGE=en
NARRATION_SPEED=1.0
NARRATION_PAUSE_MS=350
NARRATION_MAX_CHARS_PER_REQUEST=1500
NARRATION_REQUEST_TIMEOUT=300
# Conversations: troceado en bloques (ver .env.example)
NARRATION_CONVERSATION_MAX_CHARS_PER_BLOCK=4000
NARRATION_CONVERSATION_MAX_TURNS_PER_BLOCK=10
NARRATION_CONVERSATION_MIN_BLOCK_TURNS=2
NARRATION_CONVERSATION_BLOCK_RETRIES=1
```

## Archivo M4A de salida

El podcast se genera en:
```
data/narration/narrate_ai_YYMMDD_<hash>.m4a
```

Y se entrega por los canales configurados en `delivery_channels` (Telegram/Email/WhatsApp).
