# Drop-in de voces

Estructura que espera el proceso reusable `scripts/import_voice_dropin.py`:
cada voz vive en su propio subdirectorio, con un `voice.json` que la describe y
los ficheros que referencia (audio de referencia + transcripción exacta).

```
voice_dropin/
  _example/            # plantilla; los directorios que empiezan por "_" se ignoran
    voice.json
  tech_anchor_m/       # una carpeta por voz
    voice.json
    sample.wav
    sample.txt
```

Desde P6 el catálogo de voces lo posee el **gateway**; news-engine lo lee por
HTTP. El importador no escribe ningún catálogo local: crea el perfil de
operador, sube la referencia y registra el clon `qwen3_tts` contra el gateway.

## voice.json

| Campo        | Obligatorio | Descripción                                                        |
|--------------|-------------|--------------------------------------------------------------------|
| `name`       | sí          | slug estable en minúsculas (`^[a-z0-9][a-z0-9_-]*$`); nombre del perfil |
| `label`      | no          | etiqueta legible para la GUI (por defecto, el nombre)              |
| `role`       | no          | rol: `anchor` / `cohost` / `expert` / `narrator` / ...             |
| `sex`        | no          | `male` / `female` (informativo)                                    |
| `language`   | sí          | código ISO 639-1 de dos letras                                     |
| `audio`      | sí          | nombre del WAV de referencia, relativo a la carpeta de la voz      |
| `transcript` | sí          | nombre del fichero con la transcripción EXACTA de la referencia    |
| `instruct`   | no          | descripción de persona para VoiceDesign (se genera si falta)       |
| `enabled`    | no          | por defecto `true`                                                 |

## Restricciones

- Audio WAV (o cualquier formato legible por `ffprobe`).
- Duración entre 5 y 30 s.
- Transcripción de 100 a 600 caracteres y coincidente con el audio: qwen3 clona
  en modo in-context-learning y depende de ella.
- El importador es idempotente: reutiliza perfiles y clones existentes y
  reescribe la referencia con los mismos bytes.

## Uso

```bash
# Diagnóstico sin tocar el gateway (por defecto)
.venv/bin/python scripts/import_voice_dropin.py --dir ~/voice_dropin

# Importar: perfil + referencia + clon qwen3_tts
.venv/bin/python scripts/import_voice_dropin.py --dir ~/voice_dropin --apply

# Importar perfil y referencia, sin registrar el clon
.venv/bin/python scripts/import_voice_dropin.py --dir ~/voice_dropin --apply --no-clone

# Tras corregir una transcripción: borrar y volver a registrar los clones qwen3
.venv/bin/python scripts/import_voice_dropin.py --dir ~/voice_dropin --apply --reclone
```
