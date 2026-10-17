"""Tarea de limpieza de outputs caducados."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


async def cleanup_expired_outputs(
    outputs_dir: Path,
    ttl_seconds: int = 3600,
    interval_seconds: int = 300,
) -> None:
    """Elimina archivos WAV caducados de storage/outputs/."""
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            if not outputs_dir.exists():
                continue

            now = time.time()
            removed = 0
            for f in outputs_dir.glob("*.wav"):
                age = now - f.stat().st_mtime
                if age > ttl_seconds:
                    f.unlink()
                    removed += 1

            if removed > 0:
                logger.info("Cleanup: eliminados %d archivos caducados de %s", removed, outputs_dir)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error en cleanup task")


_background_task: asyncio.Task | None = None


def start_cleanup_task(outputs_dir: Path, ttl_seconds: int = 3600) -> None:
    """Inicia la tarea de limpieza en background."""
    global _background_task
    if _background_task is None or _background_task.done():
        _background_task = asyncio.create_task(cleanup_expired_outputs(outputs_dir, ttl_seconds))
        logger.info("Cleanup task iniciada (ttl=%ds, dir=%s)", ttl_seconds, outputs_dir)


def stop_cleanup_task() -> None:
    """Detiene la tarea de limpieza."""
    global _background_task
    if _background_task and not _background_task.done():
        _background_task.cancel()
        logger.info("Cleanup task detenida")
