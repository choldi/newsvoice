# Resumen de modificaciones realizadas

## Commit: `feat: rename to TTS API, add TTS_* env vars, fix GPU detection`

### 1. Cambio de nombre interno (module/package)
- `omnivoice_api/` → `tts_api/` (32 archivos renombrados)
- Todos los imports actualizados: `from omnivoice_api.*` → `from tts_api.*`

### 2. Variables de entorno renombradas (OMNIVOICE_* → TTS_*)
| Antes | Después |
|-------|---------|
| `OMNIVOICE_MODEL_ID` | `TTS_MODEL_ID` |
| `OMNIVOICE_DTYPE` | `TTS_DTYPE` |
| `OMNIVOICE_WARMUP_ON_START` | `TTS_WARMUP_ON_START` |
| `OMNIVOICE_DEVICE` | `TTS_DEVICE` |
| `OMNIVOICE_USE_MOCK` | `TTS_USE_MOCK` |
| `OMNIVOICE_FALLBACK_TO_MOCK` | `TTS_FALLBACK_TO_MOCK` |
| `OMNIVOICE_INSTALL_DIR` | `TTS_INSTALL_DIR` |
| `OMNIVOICE_PATH` | `TTS_PATH` |
| `OMNIVOICE_VENV_DIR` | `TTS_VENV_DIR` |

### 3. Branding / APP_NAME
- `APP_NAME = "OmniVoice API"` → `"TTS API"`
- Actualizado en: `settings.py`, `main.py`, `.env`, `.env.example`, `Makefile`, `README.md`, `ArchitectureReview.md`, `docs/examples/*`, `docker-compose.yml`

### 4. Fix GPU detection en `health_check` (`tts_api/core/omnivoice_engine.py:780-810`)
- Antes: usaba `torch.cuda.current_device()` (puede no coincidir con el dispositivo configurado)
- Ahora: parsea `TTS_DEVICE` (ej. "cuda:0" → device_idx=0) y usa `torch.cuda.mem_get_info(device_idx)` para reportar VRAM libre del dispositivo correcto

### 5. Configuración de build y deployment
- `pyproject.toml`: `include = ["tts_api*"]`, coverage/source, ruff/isort/mypy paths
- `Dockerfile`: `COPY tts_api/ tts_api/`, `CMD ["uvicorn", "tts_api.main:app", ...]`
- `docker-compose.yml`: env vars actualizadas, volumen `${TTS_PATH:-/opt/omnivoice}`

### 6. Tests y scripts
- Tests: imports actualizados, nombres de test methods (`test_omnivoice_device` → `test_tts_device`)
- Scripts: `scripts/check_omnivoice_install.py` imports actualizados
- `tests/load/README.md`: comando uvicorn actualizado

---

## Breaking changes para consumidores

| Área | Impacto | Migración |
|------|---------|-----------|
| **HTTP API** | **Ninguno** | No requiere acción |
| **Python package** | **Breaking** | `from omnivoice_api.*` → `from tts_api.*` |
| **Docker image** | **Breaking** | Actualizar `docker pull` y `FROM` |
| **Health check `device` field** | **Fix** | Ahora reporta dispositivo configurado correctamente |
| **Env vars** | **Breaking** | Renombrar `OMNIVOICE_*` → `TTS_*` en deploy |
