"""Pool de acceso al motor OmniVoice con semáforo."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class EnginePool:
    """Controla el acceso concurrente al motor OmniVoice.

    Usa un semáforo para limitar el número de síntesis simultáneas,
    evitando OOM en GPUs con VRAM limitada.
    """

    def __init__(self, max_concurrent: int = 2) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        self._active = 0
        self._total = 0

    async def acquire(self):
        """Adquiere un slot del pool."""
        await self._semaphore.acquire()
        self._active += 1
        self._total += 1
        logger.debug(
            "EnginePool: slot adquirido (active=%d/%d)", self._active, self._max_concurrent
        )

    def release(self) -> None:
        """Libera un slot del pool."""
        self._active -= 1
        self._semaphore.release()
        logger.debug("EnginePool: slot liberado (active=%d/%d)", self._active, self._max_concurrent)

    @property
    def active(self) -> int:
        return self._active

    @property
    def total_requests(self) -> int:
        return self._total

    @property
    def stats(self) -> dict:
        return {
            "active": self._active,
            "max_concurrent": self._max_concurrent,
            "total_requests": self._total,
        }


# Pool global singleton
_engine_pool: EnginePool | None = None


def get_engine_pool() -> EnginePool:
    global _engine_pool
    if _engine_pool is None:
        _engine_pool = EnginePool(max_concurrent=2)
    return _engine_pool
