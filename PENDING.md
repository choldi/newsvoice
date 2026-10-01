# Pendientes de Fase 7.8

Pasos que requieren credenciales de Modal o decisión explícita. La branch
`phase-7.8-modal-tts` está completa e inerte hasta ejecutarlos.

## 1. Credenciales de Modal

En el `.env` del servidor (`~/news-summary/.env`):

```bash
MODAL_ENABLED=true
MODAL_TOKEN_ID=<tu_token_id>
MODAL_TOKEN_SECRET=<tu_token_secret>
```

## 2. Levantar el worker Modal

```bash
cd ~/news-summary
git checkout phase-7.8-modal-tts
docker compose --profile modal up -d narration-modal-worker
docker compose logs -f narration-modal-worker
```

## 3. Generar las voces de referencia

```bash
docker compose exec narration-modal-worker \
  python -m src.workers.design_voice --all --upload
```

Esto diseña los 12 perfiles con Qwen3-TTS VoiceDesign, guarda los WAV en
`data/voices/refs/`, actualiza `data/voices/manifest.json` y los sube al
Modal Volume. Coste: ~$0,12 una vez.

## 4. Correr el gate de ZONOS2

```bash
python scripts/benchmark_zonos.py --all
```

Los 4 criterios deben pasar:

| Criterio | Umbral |
|---|---|
| Similitud de hablante (cosine ECAPA) | ≥ 0.75 |
| Clipping con `emotion_cfg_scale=1.5` | peak ≤ 0.99 |
| `speed=1.2` acelera | ratio ≥ 1.1 |
| Energía > 12 kHz | < 5% |

Si **pasa**: `ZONOS2_ENABLED=true` en `.env`.
Si **falla**: ZONOS2 queda inactivo; Qwen3-TTS sigue siendo el renderer.

## 5. Activar la vía B en una colección

En `collections.toml` del servidor (bind-mount, inode `461207`):

```toml
narration_tts_backend = "qwen3_tts"   # o "zonos2" si el gate pasó
narration_voice_source = "qwen_design"
```

Y en el `narration_spec` de la colección:

```toml
ai_disclosure = true   # descargo audible AI Act Art. 50(4)
```

## 6. Verificar

```bash
curl http://localhost:8000/health
docker compose logs -f narration-modal-worker
```

Comprobar que `modal_spend` registra coste y que el audio entregado tiene el
descargo audible.

## 7. Merge a main

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
  servidor, el manifiesto se regenera con ~$0,12 y los `instruct` intactos.
- El bind-mount de `collections.toml` (inode `461207`) es el pie known; no
  cambiar de branch sin revisarlo.
