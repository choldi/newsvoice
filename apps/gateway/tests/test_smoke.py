"""Test smoke para verificar que la API arranca y responde correctamente.

Estos tests NO dependen de la instalación externa de OmniVoice: las rutas
externas se redirigen a un tmp_path mediante ``_isolate_external_install``.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_endpoint_returns_ok(async_client: AsyncClient):
    """Verifica que /api/v1/health responde 200 con el engine activo."""
    response = await async_client.get("/api/v1/health")

    assert response.status_code == 200
    data = response.json()

    assert data["status"] in {"ok", "degraded"}
    assert "version" in data
    assert "device" in data
    assert "engine" in data
    assert "mode" in data


@pytest.mark.asyncio
async def test_disk_endpoint(async_client: AsyncClient):
    """Verifica que /api/v1/disk devuelve 200 con total/free/used en bytes."""
    response = await async_client.get("/api/v1/disk")

    assert response.status_code == 200
    data = response.json()

    assert data["total"] > 0
    assert data["free"] >= 0
    assert data["used"] >= 0
    assert data["free"] <= data["total"]
    assert data["used"] <= data["total"]
    assert "path" in data


@pytest.mark.asyncio
async def test_liveness_endpoint(async_client: AsyncClient):
    """Verifica que /api/v1/health/live responde 200."""
    response = await async_client.get("/api/v1/health/live")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "alive"


@pytest.mark.asyncio
async def test_readiness_endpoint_with_isolated_install(async_client: AsyncClient):
    """Verifica que /api/v1/health/ready responde 200 con la instalación aislada."""
    response = await async_client.get("/api/v1/health/ready")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["checks"]["install_dir_exists"] is True
    assert data["checks"]["venv_python_exists"] is True


@pytest.mark.asyncio
async def test_readiness_endpoint_without_install(async_client: AsyncClient, monkeypatch):
    """Verifica que /api/v1/health/ready devuelve 503 si la instalación no existe."""
    # Forzamos rutas inexistentes (get_settings() lee el entorno en cada llamada).
    monkeypatch.setenv("TTS_INSTALL_DIR", "/nope/does/not/exist")
    monkeypatch.setenv("TTS_VENV_DIR", "/nope/venv")

    response = await async_client.get("/api/v1/health/ready")

    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "not_ready"
    assert data["checks"]["install_dir_exists"] is False
