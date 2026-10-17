"""Script de verificación de la instalación de OmniVoice.

Uso:
    python scripts/check_omnivoice_install.py

Este script:
1. Verifica la instalación EXTERNA de OmniVoice usando las rutas de .env
   (OMNIVOICE_INSTALL_DIR / OMNIVOICE_VENV_DIR, o OMNIVOICE_PATH como base).
   OmniVoice NO es dependencia de este proyecto: se consume desde su propio
   venv externo (ver docs/INSTALLATION.md y ArchitectureReview.md).
2. Verifica que el python del venv externo pueda importar omnivoice.
3. Verifica que el `torch` del venv del PROYECTO (instalado vía `uv sync`
   desde el índice cu124) tenga CUDA habilitado (torch.cuda.is_available()
   debe ser True). Esto es crítico para la P2000: si torch se instaló como
   rueda CPU-only, el health check de la API reportará GPU no disponible.

Pensado para ser invocado desde el Makefile (target check-omnivoice-install)
de forma portable (Windows + Unix).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_env_file(env_path: Path | None = None) -> None:
    """Carga variables de un fichero .env en os.environ (sin sobrescribir).

    Formato soportado: líneas KEY=VALUE, ignorando comentarios (#) y líneas
    vacías. Las comillas simples/dobles alrededor del valor se eliminan.
    """
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


def resolve_external_paths() -> tuple[Path, Path]:
    """Resuelve install_dir y venv_dir de la instalación externa.

    Prioridad:
      1. OMNIVOICE_INSTALL_DIR / OMNIVOICE_VENV_DIR (explícitas)
      2. OMNIVOICE_PATH como base: <base>/OMNIVOICE y <base>/omnivoice_env
      3. Defaults de engine_paths para install_dir y venv hermano.
    """
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    from tts_api.core.engine_paths import (
        default_install_dir,
        python_bin_from_venv,
    )

    base = os.environ.get("OMNIVOICE_PATH")

    install_env = os.environ.get("OMNIVOICE_INSTALL_DIR")
    if install_env:
        install_dir = Path(install_env)
    elif base:
        install_dir = Path(base) / "OMNIVOICE"
    else:
        install_dir = default_install_dir()

    venv_env = os.environ.get("OMNIVOICE_VENV_DIR")
    if venv_env:
        venv_dir = Path(venv_env)
    elif base:
        venv_dir = Path(base) / "omnivoice_env"
    else:
        # Convención documentada: el venv es hermano del código fuente.
        venv_dir = install_dir.parent / "omnivoice_env"

    # Validación temprana: el python del venv debe existir.
    python_bin_from_venv(venv_dir)

    return install_dir, venv_dir


def check_external_install() -> bool:
    """Verifica la instalación externa de OmniVoice (código + venv)."""
    print("(1) Verificando instalación externa de OmniVoice...")

    load_env_file()

    try:
        install_dir, venv_dir = resolve_external_paths()
    except Exception as exc:
        print(f"  ERROR: {exc}")
        print(
            "  Define OMNIVOICE_INSTALL_DIR y OMNIVOICE_VENV_DIR en .env "
            "o OMNIVOICE_PATH apuntando a la base de la instalación externa."
        )
        return False

    print(f"  OMNIVOICE_INSTALL_DIR = {install_dir}")
    print(f"  OMNIVOICE_VENV_DIR    = {venv_dir}")

    if not install_dir.is_dir():
        print(f"  ERROR: no existe el directorio de instalación: {install_dir}")
        return False

    from tts_api.core.engine_paths import python_bin_from_venv

    try:
        python_bin = python_bin_from_venv(venv_dir)
    except Exception as exc:
        print(f"  ERROR: {exc}")
        return False

    print(f"  OK: Python del venv externo = {python_bin}")

    # Verificar que el venv externo puede importar omnivoice
    check_cmd = "import omnivoice; print(f'  omnivoice importado OK desde: {omnivoice.__file__}')"
    result = subprocess.run(
        [str(python_bin), "-c", check_cmd],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="")
    if result.returncode != 0:
        print("  ERROR: el venv externo no puede importar omnivoice")
        return False

    return True


def check_torch_cuda() -> bool:
    """Verifica que torch está instalado con CUDA habilitado.

    Crítico para la P2000: si torch se instaló como rueda CPU-only
    (p.ej. por no usar el índice cu124), torch.cuda.is_available()
    devolverá False y el engine no podrá usar la GPU.
    """
    print("(2) Verificando que torch tiene CUDA habilitado...")
    try:
        import torch
    except ImportError:
        print("  ERROR: torch no está instalado en el venv actual")
        print("  Ejecuta `make install` (uv sync) para instalarlo con CUDA 12.4")
        return False

    cuda_available = torch.cuda.is_available()
    cuda_version = torch.version.cuda
    torch_version = torch.__version__

    print(f"  torch version        = {torch_version}")
    print(f"  torch.version.cuda   = {cuda_version}")
    print(f"  torch.cuda.is_available() = {cuda_available}")

    if not cuda_available:
        print(
            "  ERROR: torch se instaló SIN CUDA. La P2000 no podrá usarse.\n"
            "  Causa probable: no se usó el índice cu124 al instalar.\n"
            "  Solución: ejecuta `make install` (uv lee [tool.uv] del pyproject.toml\n"
            "  y añade automáticamente https://download.pytorch.org/whl/cu124)."
        )
        return False

    gpu_count = torch.cuda.device_count()
    print(f"  GPUs detectadas      = {gpu_count}")
    for i in range(gpu_count):
        print(f"    GPU {i}: {torch.cuda.get_device_name(i)}")

    print("  OK: torch tiene CUDA habilitado")
    return True


def main() -> int:
    """Ejecuta las verificaciones. Devuelve 0 si todo OK, 1 si hay error."""
    print("Verificando instalación de OmniVoice...")

    if not check_external_install():
        print("\nERROR: No se encontró la instalación externa de OmniVoice")
        print("Opciones:")
        print("  1. Define OMNIVOICE_INSTALL_DIR y OMNIVOICE_VENV_DIR en .env")
        print("  2. O define OMNIVOICE_PATH apuntando a la base (p.ej. C:\\AI\\TTS\\OMNIVOICE)")
        print("  Ver docs/INSTALLATION.md para más detalles.")
        return 1
    print("\nOmniVoice verificado correctamente (instalación externa)")

    # Verificar CUDA (crítico para la P2000)
    if not check_torch_cuda():
        return 1

    print("\nTodo OK: OmniVoice externo + torch con CUDA 12.4 listos para usar la P2000")
    return 0


if __name__ == "__main__":
    sys.exit(main())
