"""Utilidades para resolver paths de la instalación externa de OmniVoice.

Este módulo NO depende de pydantic-settings para poder usarse en scripts
de verificación (check-omnivoice-install) antes de que la app arranque.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _get_env_path(key: str, default: Path | None = None) -> Path | None:
    """Obtiene un path desde variable de entorno, expandiendo ~ y variables."""
    value = os.environ.get(key)
    if value:
        return Path(value).expanduser().resolve()
    return default


def default_install_dir() -> Path:
    """Directorio por defecto de la instalación externa de OmniVoice.

    Orden de prioridad:
    1. TTS_INSTALL_DIR
    2. TTS_PATH (alias compatibilidad)
    3. Directorio actual / omnivoice (fallback para desarrollo)
    """
    return (
        _get_env_path("TTS_INSTALL_DIR") or _get_env_path("TTS_PATH") or (Path.cwd() / "omnivoice")
    )


def default_venv_dir() -> Path:
    """Directorio por defecto del venv externo de OmniVoice.

    Orden de prioridad:
    1. TTS_VENV_DIR
    2. {INSTALL_DIR}/.venv
    """
    install_dir = default_install_dir()
    return _get_env_path("TTS_VENV_DIR") or (install_dir / ".venv")


def python_bin_from_venv(venv_dir: Path) -> Path:
    """Ruta al python.exe/python del venv dado."""
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def load_env_file(env_path: Path | None = None) -> None:
    """Carga variables desde un archivo .env (simple, sin dependencias).

    Soporta:
    - KEY=VALUE
    - export KEY=VALUE
    - Comentarios con #
    - Comillas simples/dobles
    """
    if env_path is None:
        env_path = Path.cwd() / ".env"

    if not env_path.exists():
        return

    with env_path.open(encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def validate_installation(install_dir: Path, venv_dir: Path) -> Path:
    """Valida que la instalación externa existe y devuelve el python bin.

    Raises:
        FileNotFoundError: Si falta algún directorio o el python bin.
    """
    if not install_dir.exists():
        raise FileNotFoundError(f"Directorio de instalación no existe: {install_dir}")
    if not venv_dir.exists():
        raise FileNotFoundError(f"Directorio del venv no existe: {venv_dir}")

    python_bin = python_bin_from_venv(venv_dir)
    if not python_bin.exists():
        raise FileNotFoundError(f"Python no encontrado en venv: {python_bin}")

    return python_bin
