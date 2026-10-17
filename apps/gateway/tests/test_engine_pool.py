"""Tests unitarios para el pool de acceso al motor OmniVoice."""

from __future__ import annotations

import asyncio

import pytest

from tts_api.core.engine_pool import EnginePool, get_engine_pool


class TestEnginePool:
    """Tests para EnginePool con semáforo."""

    def test_initial_state(self) -> None:
        """Test de estado inicial del pool."""
        pool = EnginePool(max_concurrent=3)
        assert pool.active == 0
        assert pool.total_requests == 0
        assert pool.stats == {
            "active": 0,
            "max_concurrent": 3,
            "total_requests": 0,
        }

    @pytest.mark.asyncio
    async def test_acquire_and_release(self) -> None:
        """Test de adquisición y liberación de slots."""
        pool = EnginePool(max_concurrent=2)

        await pool.acquire()
        assert pool.active == 1
        assert pool.total_requests == 1

        await pool.acquire()
        assert pool.active == 2
        assert pool.total_requests == 2

        pool.release()
        assert pool.active == 1

        pool.release()
        assert pool.active == 0

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self) -> None:
        """Test de que el semáforo limita la concurrencia."""
        pool = EnginePool(max_concurrent=1)
        results = []

        async def worker(worker_id: int) -> None:
            await pool.acquire()
            results.append(f"start-{worker_id}")
            await asyncio.sleep(0.05)
            results.append(f"end-{worker_id}")
            pool.release()

        # Lanzar 2 workers — el segundo debe esperar
        await asyncio.gather(worker(1), worker(2))

        # El primero debe terminar antes de que el segundo empiece
        assert results == ["start-1", "end-1", "start-2", "end-2"]

    @pytest.mark.asyncio
    async def test_stats_updated_correctly(self) -> None:
        """Test de que las estadísticas se actualizan correctamente."""
        pool = EnginePool(max_concurrent=2)

        await pool.acquire()
        await pool.acquire()
        pool.release()

        stats = pool.stats
        assert stats["active"] == 1
        assert stats["max_concurrent"] == 2
        assert stats["total_requests"] == 2


class TestGetEnginePool:
    """Tests para el singleton global del pool."""

    def test_returns_singleton(self) -> None:
        """Test de que retorna la misma instancia."""
        # Reset global state
        import tts_api.core.engine_pool as mod

        mod._engine_pool = None

        pool1 = get_engine_pool()
        pool2 = get_engine_pool()
        assert pool1 is pool2

    def test_default_max_concurrent(self) -> None:
        """Test de que el pool por defecto tiene max_concurrent=2."""
        import tts_api.core.engine_pool as mod

        mod._engine_pool = None

        pool = get_engine_pool()
        assert pool._max_concurrent == 2
