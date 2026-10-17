"""Descarga el modelo OmniVoice desde HuggingFace Hub.

Uso:
    python scripts/download_model.py

Descarga el modelo ModelsLab/omnivoice-singing (con emociones y singing)
al directorio especificado en OMNIVOICE_MODEL_PATH o {OMNIVOICE_INSTALL_DIR}/models.

Requiere: huggingface_hub (se instala con omnivoice).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_env_file(env_path: Path | None = None) -> None:
    """Carga variables de un fichero .env en os.environ (sin sobrescribir)."""
    env_path = env_path or PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def resolve_model_dir() -> Path:
    """Resuelve el directorio donde se almacenará el modelo.

    Prioridad:
      1. OMNIVOICE_MODEL_PATH
      2. OMNIVOICE_INSTALL_DIR/models
      3. ./models (fallback local)
    """
    model_path = os.environ.get("OMNIVOICE_MODEL_PATH")
    if model_path:
        return Path(model_path).expanduser().resolve()

    install_dir = os.environ.get("OMNIVOICE_INSTALL_DIR")
    if install_dir:
        return Path(install_dir).expanduser().resolve() / "models"

    return PROJECT_ROOT / "models"


def download_model() -> bool:
    """Descarga el modelo desde HuggingFace Hub. Devuelve True si ok."""
    load_env_file()

    model_id = os.environ.get("OMNIVOICE_MODEL_ID", "ModelsLab/omnivoice-singing")
    model_dir = resolve_model_dir()

    print(f"Modelo:    {model_id}")
    print(f"Destino:   {model_dir}")

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print(
            "ERROR: huggingface_hub no está instalado.\nInstálalo con: pip install huggingface_hub"
        )
        return False

    model_dir.mkdir(parents=True, exist_ok=True)

    print(f"Descargando modelo {model_id}...")
    try:
        path = snapshot_download(
            repo_id=model_id,
            local_dir=str(model_dir),
        )
        print(f"Modelo descargado en: {path}")
    except Exception as e:
        print(f"ERROR descargando modelo: {e}")
        return False

    # Verificar archivos críticos
    critical_files = ["config.json", "model.safetensors", "tokenizer.json"]
    missing = [f for f in critical_files if not (model_dir / f).exists()]
    if missing:
        print(f"ADVERTENCIA: Archivos faltantes: {missing}")
        return False

    print("OK: Modelo descargado correctamente")
    return True


def main() -> int:
    return 0 if download_model() else 1


if __name__ == "__main__":
    sys.exit(main())
