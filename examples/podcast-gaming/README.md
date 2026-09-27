# Podcast Gaming - Configuración de ejemplo

Este directorio contiene los archivos necesarios para configurar y ejecutar el podcast
diario de videojuegos "Game Over" usando el news-engine.

## Contenido

| Archivo | Descripción |
|---------|-------------|
| `collections.gaming.toml` | Configuración completa de la colección `gaming` lista para copiar a `collections.toml` |
| `narration-spec.json` | Especificación de narración para dos hosts (Dwight + MichaelScott) |
| `narration-script.example.json` | Ejemplo de script de narración para render manual con `--script` |
| `check_feeds.py` | Verificador de operatividad de los 4 feeds RSS |

## Verificación de feeds (comprobada 2026-09-27)

Todos los feeds responden **HTTP 200** y devuelven items válidos:

```
IGN       → 20 items  (https://feeds.feedburner.com/ign/all)
Polygon   → 10 items  (https://www.polygon.com/rss/index.xml)
Kotaku    → 20 items  (https://www.kotaku.com/rss)
VG247     → 100 items (https://www.vg247.com/feed)
Total:    → ~150 items por ejecución
```

## Uso

### 1. Copiar la configuración al servidor

```bash
# En el servidor de despliegue (ej. 192.168.5.7:/home/jordi/news-summary)
cp examples/podcast-gaming/collections.gaming.toml collections.toml
# O añadir el bloque [[collections]] al collections.toml existente
```

### 2. Registrar el schedule en Temporal

```bash
# Desde el contenedor worker
docker compose exec worker python -m src.workers.schedule --collection gaming
```

### 3. Ejecución manual (para probar)

```bash
# Disparar el digest pipeline
docker compose exec worker python -m src.workers.trigger --collection gaming

# Disparar solo la narración (requiere digest previo)
docker compose exec worker python -m src.workers.trigger_narration \
    --collection gaming \
    --spec examples/podcast-gaming/narration-spec.json \
    --script examples/podcast-gaming/narration-script.example.json
```

### 4. Verificar feeds localmente

```bash
python examples/podcast-gaming/check_feeds.py
```

## Voces usadas (clonadas en OmniVoice)

| Rol | Voz | UUID | Engine | Idioma |
|-----|-----|------|--------|--------|
| Michael (host entusiasta) | MichaelScott | `3e47fbcc-53a8-4551-be91-48f93d62a19f` | pocket_tts | en |
| Dwight (analista deadpan) | Dwight | `f0c280e3-0b2d-4f91-9ad6-ff0f86cf49f8` | pocket_tts | en |

> **Nota**: La voz se llama **Dwight** (no "Dwaigt"). Ambas están en inglés y usan PocketTTS.

## Configuración clave

- **Horario**: 10:00 UTC diario
- **Estructura**: 5 historias principales (extendidas, 5-6 frases) + 10 titulares
- **Idioma**: Inglés (`en`)
- **Estrategia narración**: `turns` (diálogo entre dos voces distintas)
- **Música**: Ninguna
- **Entrega narración**: Habilitada (`narration_deliver_audio = true`)

## Variables de entorno relevantes en el servidor

```bash
NARRATION_ENABLED=true
NARRATION_API_BASE_URL=http://debwk.elver-exponential.ts.net:8000/api/v1
NARRATION_TASK_QUEUE=news-engine-narration-task-queue
NARRATION_FALLBACK_TO_EDGE=true  # General (single-voice) fallback stays enabled
NARRATIONS_DIALOG_FALLBACK_TO_EDGE=false  # Important: prevents silent substitution of cloned voices in dialogs
NARRATION_LANGUAGE=en
NARRATION_SPEED=1.0
NARRATION_PAUSE_MS=350
NARRATION_MAX_CHARS_PER_REQUEST=1500
```

## Archivo M4A de salida

El podcast se genera en:
```
data/narration/narrate_gaming_YYMMDD_<hash>.m4a
```

Y se entrega por los canales configurados en `delivery_channels` (Telegram/Email/WhatsApp).