"""Router para endpoints de sistema y operaciones (públicos)."""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter

from tts_api.settings import get_settings

router = APIRouter(tags=["system"])


@router.get(
    "/disk",
    response_model=dict,
    summary="Consultar espacio en disco",
    description=(
        "Devuelve el uso de disco del directorio de salida (OUTPUTS_DIR) en bytes. "
        "Endpoint público para que sistemas externos (ej. monitor) lo consulten."
    ),
)
async def get_disk() -> dict:
    """Consulta el uso de disco del directorio de salida.

    Returns:
        Diccionario con total, free y used en bytes.
    """
    target = get_settings().OUTPUTS_DIR

    # Asegurar que la ruta exista para poder medir el punto de montaje.
    probe_dir: Path = target if target.is_dir() else target.parent
    for candidate in (target, target.parent, Path.cwd()):
        if candidate.exists():
            probe_dir = candidate
            break

    usage = shutil.disk_usage(probe_dir)
    return {
        "path": str(probe_dir),
        "total": usage.total,
        "free": usage.free,
        "used": usage.used,
    }
