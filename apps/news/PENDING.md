# Pendientes de Fase 7.9 (diseño de voces en debwk)

Pasos operativos. La implementación está en la branch `phase-7.8-modal-tts` y
es inerte hasta ejecutarlos: **la vía A (OmniVoice + clones) no cambia en
ningún paso** y el render diario sigue igual.

**Estado (2026-10-02):** pasos 1-5.b hechos y verificados en producción —
namespace, `voice-catalog-worker`, design worker en debwk, modelo VoiceDesign
y las 24 voces (`modal_cost_usd: 0.0`); staging desplegado y probado con 1
perfil y con un lote de 3 (`status: ok`, sin `TMPRL1103` en el historial).
Quedan 6-10 (credenciales Modal, gate ZONOS2, vía B, merge). Los texto de
abajo describen cómo quedó el sistema, no lo que falta.

**Revisión de 6-10 (2026-10-30):**

- **6 · Credenciales Modal: hecho en debwk** (`MODAL_TOKEN_ID`/`SECRET` en
  `apps/news/.env`, `narration-modal-worker` en marcha). `MODAL_ENABLED` sigue
  en `false` **a propósito**: el gasto Modal ahora es por operación (switches
  `run_in_modal` de narración/lecciones/audiolibros y Phase 10.4 de voces;
  los audiolibros rechazan Modal). Para gastar, activa `MODAL_ENABLED=true`
  en la operación concreta.
- **7 · Gate ZONOS2 y 8 · Gate VoxCPM2: sin ejecutar y opcionales** — solo
  hacen falta para activar vía B (`zonos2`/`voxcpm2` por colección). Hoy
  ninguna colección los declara; mantener dormidos salvo que se quiera el
  producto vía B.
- **9 · Activar vía B: no aplica aún** — `tech` usa `qwen3_tts` **local**
  (`run_in_modal=false`), el resto edge/omni_voice.
- **10 · Verificar: condicional** — solo después del paso 9.
- **11 · Merge: obsoleto** — el merge al monorepo y el cutover de debwk ya se
  hicieron (MERGE_PLAN P0-P10, 2026-10-19); la rama `phase-7.8-modal-tts` y su
  PR ya no existen.

## 1. Namespace en el servidor (una vez)

Un workflow y sus actividades viven en el mismo namespace, así que las dos
mitades del catálogo (`--role catalog` y `--role design`) se conectan a
`news-engine-voices`. En local ya existe; **falta en producción**:

```bash
cd ~/news-summary
./scripts/create_namespaces.sh   # crea los tres namespaces, idempotente
```

Se pierde si se restaura `temporal_pg_data` (vive en Postgres, no en el repo).

## 2. Catalog worker (servidor)

```bash
docker compose up -d voice-catalog-worker
docker compose logs -f voice-catalog-worker
```

Sin él, el trigger de la admin GUI arranca un workflow que nadie coge.

## 3. Design worker (debwk)

Setup completo en `Deploy.md` §8.3: clone en `~/news-summary`, `temporalio +
structlog + pydantic-settings` en `~/qwen3-tts/venv`, unit systemd de usuario
con `TEMPORAL_HOST=192.168.5.7:7233`, `loginctl enable-linger "$USER"`.

```bash
systemctl --user status news-engine-voice-worker
journalctl --user -fu news-engine-voice-worker
nc -vz 192.168.5.7 7233
```

## 4. Descargar el modelo VoiceDesign (~4 GB)

La caja solo tiene `Qwen3-TTS-12Hz-1.7B-CustomVoice` en la caché de
HuggingFace. La primera ejecución lo descarga sola, o de forma explícita:

```bash
huggingface-cli download Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign
```

Si la caché no tiene sitio, `HF_HOME` apunta a un disco grande en la unit.

## 5. Generar las voces

```bash
docker compose exec voice-catalog-worker \
  python -m src.workers.design_voice --all --dry-run    # qué haría
docker compose exec voice-catalog-worker \
  python -m src.workers.design_voice --name anchor_neutral_m
docker compose exec voice-catalog-worker \
  python -m src.workers.design_voice --all --upload
```

Diseña los 24 perfiles (16 en + 8 es) con Qwen3-TTS VoiceDesign en la GPU box,
guarda los WAV en `data/voices/refs/`, actualiza `data/voices/manifest.json`
(`modal_cost_usd: 0.0`, coste real $0) y los sube al Modal Volume. Coste:
**$0** — ya no se diseña en Modal.

Comprobar `GET /admin/voices/catalog`: 24 perfiles y `ref_text_sha256` igual al
de `voice_profiles.json`.

## 5.b Staging de audio (clave compartida)

El diseño deja de pasar el WAV por el historial de Temporal: la activity lo
sube a `POST /api/internal/voices/staged/{name}` en el servidor y la store
activity lo lee de `data/voices/.staging/`. Mientras no haya clave, el
endpoint responde 503 y ningún perfil puede completarse.

```bash
# servidor: una vez
python -c "import secrets; print(secrets.token_urlsafe(32))"
echo "VOICE_STAGING_API_KEY=<valor>" >> ~/news-summary/.env
docker compose up -d api voice-catalog-worker

# debwk: la misma clave en la unit (deploy/§8.3)
systemctl --user edit news-engine-voice-worker   # o editar la unit directa
systemctl --user restart news-engine-voice-worker
```

Comprobar en los logs de la API el evento (una petición por voz) y en los del
design worker la síntesis, con `staged_bytes` en la línea:

```bash
docker compose logs api | grep voice_reference_staged     # {"bytes": ..., "name": ...}
docker compose logs voice-catalog-worker | grep "Voice stored"
journalctl --user -u news-engine-voice-worker | grep "Voice designed"   # en debwk
ls data/voices/.staging/                                  # vacío tras el store
```

Desde `3d74687` el bootstrap de los workers enciende los loggers del SDK de
Temporal (`configure_temporal_logging()` en `src/workers/client.py`), así que
estas líneas llegan a `docker logs` y a `journalctl` con el contexto del
workflow/activity entre paréntesis. El CLI imprime además
`results[].status == "ok"`, y los fallos se ven ahí mismo o en la Temporal UI.

## 6. Credenciales de Modal (solo si se activa la vía B)

El upload del Volume y el render Qwen/ZONOS2 siguen en Modal; el diseño ya no.
En el `.env` del servidor (`~/news-summary/.env`):

```bash
MODAL_ENABLED=true
MODAL_TOKEN_ID=<tu_token_id>
MODAL_TOKEN_SECRET=<tu_token_secret>
```

Y levantar el worker Modal:

```bash
docker compose --profile modal up -d narration-modal-worker
```

## 7. Correr el gate de ZONOS2

```bash
python scripts/benchmark_zonos.py --all
```

Los 4 criterios deben pasar:

| Criterio | Umbral |
|---|---|
| Similitud de hablante (cosine ECAPA) | ≥ 0.75 |
| Clipping en modo expresivo | peak ≤ 0.99 |
| `speed=1.2` acelera | ratio ≥ 1.1 |
| Energía > 12 kHz | < 5% |

Si **pasa**: `ZONOS2_ENABLED=true` en `.env`.
Si **falla**: ZONOS2 queda inactivo; Qwen3-TTS sigue siendo el renderer.

### Notas de la puesta en marcha de ZONOS2 en Modal

- **GPU**: `sgl_kernel` (Mini-SGLang) solo trae kernels `sm90`/`sm100`, así que
  ZONOS2 **no corre en L4 (SM89)**; la clase usa `gpu="H100"`.
- **Imagen**: base `nvidia/cuda:12.8.1-devel-ubuntu22.04` (nvcc hace falta para
  el JIT de los kernels) + `ffmpeg` (decodifica los WAV de referencia) +
  `libnuma1` (sgl_kernel) y `CUDA_HOME=/usr/local/cuda`.
- **Idioma**: `generate_one(language=...)` valida contra los códigos del servidor
  (`fr_fr`, `en_us`, ...), no ISO 639-1.
- **Emoción**: la API offline `TTSLLM.generate_one` **no** acepta
  `emotion_enabled`/`emotion_sliders`/`emotion_cfg_scale`; el delta de emoción
  solo se inyecta en el transporte servidor. La clase acepta `emotion` por
  paridad pero lo ignora, y el gate mide el peak de una toma en modo expresivo
  en su lugar.


## 8. Correr el gate de VoxCPM2

VoxCPM2 es Apache-2.0, 48 kHz y corre en Modal (L4). Se activa por colección
solo si supera el gate de calidad.

```bash
python scripts/benchmark_voxcpm.py --all
```

Los criterios deben pasar:

| Criterio | Umbral |
|---|---|
| `sample_rate` (frecuencia nativa) | == 48000 |
| `no_clipping` (pico sin recorte) | peak ≤ 0.99 |
| `mos` (DNSMOS P.835 OVRL) | ≥ 3.0 |
| `wideband` (energía > 16 kHz) | ≥ 0.02 |

El script también informa `best_gain` (mejora MOS entre mejor take y el primero)
cuando se usan varios candidatos (`VOXCPM2_CANDIDATES`, por defecto 3).

Si **pasa**: `VOXCPM2_ENABLED=true` en `.env`.
Si **falla**: VoxCPM2 queda inactivo; el backend por defecto sigue siendo
`qwen3_tts` (o el que esté activo para esa colección).

## 9. Activar la vía B en una colección

En `collections.toml` del servidor (bind-mount, inode `461207`):

```toml
narration_tts_backend = "qwen3_tts"   # o "zonos2" / "voxcpm2" si el gate pasó
narration_voice_source = "qwen_design"
```

Y en el `narration_spec` de la colección:

```toml
ai_disclosure = true   # descargo audible AI Act Art. 50(4)
```

## 10. Verificar

```bash
curl http://localhost:8000/health
docker compose logs -f voice-catalog-worker
docker compose logs -f narration-modal-worker
```

Comprobar que `modal_spend` registra coste (solo render), que el audio
entregado tiene el descargo audible y que la vía A no ha cambiado.

## 11. Merge a main

Cuando sea estable:

```bash
gh pr create --base main --head phase-7.8-modal-tts
```

Después de mergear, el servidor vuelve a `main`:

```bash
cd ~/news-summary && git checkout main && docker compose up -d
```

## Notas

- La vía A (OmniVoice + clones) no cambia en ningún paso.
- `data/*` está gitignored: los WAV generados no se commitean. Si pierdes el
  servidor, se regeneran en la GPU box sin coste, con los `instruct` y
  `ref_segments` intactos.
- El bind-mount de `collections.toml` (inode `461207`) es el pie known; no
  cambiar de branch sin revisarlo (commit `80a19a5`).
- **Hueco heredado, corregido en la Fase 7.9b**: `narration_local_task_queue` no
  lo arrancaba nadie en docker-compose (solo se resolvió con `--queue`), así que
  el worker local escuchaba `narration_task_queue` mientras
  `admin_narration` / `trigger_narration` apuntaban al otro nombre y la
  narración local encolaba para siempre. Ahora
  `narration_local_task_queue == narration_task_queue`
  (`news-engine-narration-task-queue`), los workers locales y Modal viven además
  en namespaces propio (`news-engine-podcast-local`,
  `news-engine-podcast-modal`) y el dispatch cross-namespace lo resuelve
  `dispatch_narration_activity`. Cubierto por
  `tests/unit/test_podcast_target.py` y
  `tests/unit/test_dispatch_narration_activity.py`.
