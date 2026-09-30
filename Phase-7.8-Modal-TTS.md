# Phase 7.8 — Voz pública con Qwen3-TTS en Modal

> **ESTADO: EN DESARROLLO** en la branch `phase-7.8-modal-tts`. Nada está
> desplegado. La vía A (OmniVoice + clones actuales) permanece intacta.
> Documento de diseño aprobado el 2026-09-30.

## Objetivo

Añadir en paralelo a la vía privada actual una **vía pública** con derechos
comerciales claros, usando un LLM y un TTS en Modal, con control de
presupuesto duro y un catálogo de voces propio.

## Decisiones cerradas

| # | Decisión |
|---|---|
| 1 | `TTSBackend` = `OMNIVOICE` \| **`QWEN3_TTS`** \| `ZONOS2` (ZONOS2 behind gate) |
| 2 | `VoiceSource` = **`QWEN_DESIGN`** \| `RECORDING`, ambos desde el día 1 |
| 3 | VoiceDesign = Modal one-off, mismo proveedor |
| 4 | Catálogo: **8 inglés + 4 español**, JSON versionable, 12 perfiles |
| 5 | Local = fuente de verdad → subida al Modal Volume |
| 6 | Dos task queues: **local** + **modal** |
| 7 | Worker modal **creado pero NO levantado** |
| 8 | Descargo AI Act audible, interruptor por colección |
| 9 | `atempo` para `speed` solo en `QWEN3_TTS` |
| 10 | Presupuesto **$1/día / $10/mes**, cuatro capas |
| 11 | `modal` como única dependencia nueva |

## Arquitectura

```
DigestWorkflow ──► task_queue desde PipelineConfig.narration
                        │
        ┌───────────────┴────────────────┐
   local queue                      modal queue (NO levantado)
   render_narration_activity         render_narration_activity
   → gateway OmniVoice CPU           → .spawn() a Modal L4
                                     design_voice_activity
                                     upload_voice_activity
                        │
   VoiceCatalogWorkflow ──► por perfil, independiente
        │                          │
        ├─► data/voices/refs/<name>.wav     fuente de verdad
        ├─► data/voices/manifest.json
        └─► upload ──► Modal Volume ──► ZONOS2 / Qwen-Base
```

Enrutado en `schedule.py:61` por `collection.narration_tts_backend`:
`OMNIVOICE` → local, resto → modal.

## Los 12 perfiles

`voice_profiles.json` en la raíz, versionable. Todos describen **atributos
vocales, nunca una persona real**.

**Inglés** — `anchor_neutral_m`, `anchor_neutral_f`, `cohost_warm_f`,
`narrator_calm_m`, `analyst_measured_m`, `reporter_bright_f`,
`commentator_deep_m`, `narrator_neutral_f`

**Español** — `es_anchor_neutral_m`, `es_anchor_neutral_f`,
`es_narrator_calm_m`, `es_cohost_warm_f`

Cada uno: `name`, `label`, `language`, `instruct` completo, `ref_text` de
250–320 chars (~14–18 s, satisface la ventana de 10–30 s de ZONOS2 y el
mínimo de 3 s de Qwen), `role_hint`, `enabled`.

## Guard anti-drift

`manifest.json` guarda `instruct_sha256` y `ref_text_sha256` junto a cada WAV.
Si el perfil cambia y el audio no, la entrada se marca `stale`, `--dry-run` lo
lista y el render avisa en vez de clonar en silencio con un `ref_text`
desalineado.

## Cambios

**Colas** — `config.py`: `narration_local_task_queue`,
`narration_modal_task_queue`. `schedule.py:61` enruta. `run_narration_worker.py`
gana `--queue`. `run_modal_worker.py` nuevo (mismo `Dockerfile`, no levantado).
`docker-compose.yml`: servicio declarado con perfil inactivo. Ambas colas
registran las mismas actividades de narración — el `NotFoundError` que advierte
`narration_activities.py:3-8` lo exige.

**Catálogo** — `src/api/models/voices.py`, `src/core/voice_catalog.py`,
`voice_profiles.json`.

**Flujo** — `src/workers/voice_activities.py` (`design_voice_activity`,
`upload_voice_activity`), `src/workers/voice_workflows.py`
(`VoiceCatalogWorkflow`), `scripts/design_voice.py` con
`--all | --name X | --only-missing | --dry-run`. Perfil fallido no tumba el lote.

**TTS** — `narration_tts.py`: `SIDECAR_VERSION = 2` (`:82`), `cache_probe()`
extraído de `:1296`, dispatch en `synthesize_turn()` (`:799`), batch en
`_render_turns_locally()` (`:1220`), `atempo`, descargo, sidecar (`:1363`),
rechazo `conversations` en `:1322`. **Sin tocar**: `content_hash()` (`:507`) y
`_to_canonical()` (`:1032`). `models/narration.py`: `TTSBackend`, `VoiceSource`,
4 campos en `NarrationSpec` (`:123`), 3 en `NarrationOut` (`:384`).

**Presupuesto** — `009_modal_spend.sql`, gates en `modal_tts_activity`, check
`modal_budget` en `monitoring/checks.py`, 9 settings nuevos.

**Config de colección** — `narration_tts_backend` + `narration_voice_source` en
`core/collections.py:82`, `collections_repo.py:32`, `routes_ui.py`,
`admin_collections.py`.

**Admin** — `src/api/routes/admin_voices.py`.

**Tests** — catálogo (JSON roto, `ref_text` ausente, duplicados), drift,
round-trip de manifiesto, fan-out con stubs, aislamiento de fallos, `cache_probe`,
gates de presupuesto, cooldown, `atempo`, descargo, `conversations` rejection,
`terminate` en `finally`, y **regresión de vía A**.

**Docs** — `.env.example`, `Deploy.md`, `Phases.md`, `collections.toml` (repo y
servidor, inode `461207`).

## Descartado

- **HF serverless**: `AutoModel.register` en runtime, sin `auto_map` → no soportado.
- **HF Endpoint**: viable pero $0,01–0,50 por un one-off de $0,01, dos proveedores,
  y no elimina la GPU.
- **DashScope**: anotado como `voice_source_host` futuro para iterar el prompt en
  el navegador.

## Compliance

- Descargo audible por defecto en vía B; interruptor por colección.
- **`VoiceSource.RECORDING` exige consentimiento escrito** y arrastra `ref_text`
  obligatorio (sin él, `x_vector_only_mode=True` y calidad degradada). Es donde
  vuelve el riesgo que el diseño sintético eliminaba.
- Sin derechohabientes en `QWEN_DESIGN` → sin Rightsholders Disclosure.

## Coste

12 perfiles × VoiceDesign ≈ **$0,12** una vez. Operativo ~$0,10/día sobre topes
de $1/día y $10/mes.

## Orden de ejecución

1. Modelos + catálogo + anti-drift — sin GPU
2. `cache_probe` + dispatch + batch + `atempo` + descargo — sin GPU
3. Migración `009` + capas 1-3 de presupuesto — sin GPU
4. Check `modal_budget` + capa 4
5. `run_modal_worker.py` + docker-compose **sin levantar**
6. `voice_activities` + `voice_workflows` + `design_voice.py`
7. `admin_voices` + docs + `Phases.md` + commit
8. Gate ZONOS2 — aparte, al final

**Pasos 1-4 verificables sin GPU ni credenciales.**

## Efectos colaterales

- `data/*` está gitignored (`.gitignore:28`), así que los 12 perfiles se versionan
  pero sus WAV no — si pierdes el servidor, el manifiesto se regenera con $0,12
  y los `instruct` intactos.
- Al desplegar, `tts_backend` entra en el hash, así que **la caché se invalida
  una vez y vía A re-renderiza**: tus amigos recibirán un audio nuevo ese día, y
  el normal desde el siguiente.
- `collections.toml` va bind-mounted en el servidor (inode `461207`) y ya hubo un
  incidente por eso (commit `80a19a5`). Cambiar de branch sin revisar ese bind
  mount es el pie known.

## Garantía de seguridad

Hasta que no pongas `tts_backend = "qwen3_tts"` en una colección, la branch
entera es inerte: no hay `.spawn()`, no hay gasto, `schedule.py` enruta a la
cola local y **el audio que reciben tus amigos no cambia**. El único cambio
audible en todo el trabajo será el del descargo AI Act, y solo si lo activas.
