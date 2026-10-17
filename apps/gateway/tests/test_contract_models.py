"""The shared tts-contract package is what the gateway serves.

A response that no longer parses into the shared model is a contract break
before any client notices.
"""

from fastapi.testclient import TestClient
from tts_contract import DiskUsage, EnginesResponse

from tts_api.main import app


def test_disk_response_matches_the_shared_contract() -> None:
    """``GET /api/v1/disk`` parses into the shared ``DiskUsage`` model."""
    resp = TestClient(app).get("/api/v1/disk")
    assert resp.status_code == 200

    disk = DiskUsage.model_validate(resp.json())

    assert disk.total > 0
    assert disk.free >= 0
    assert disk.path


def test_engines_response_matches_the_shared_contract() -> None:
    """``GET /api/v1/engines`` parses into the shared ``EnginesResponse``."""
    resp = TestClient(app).get("/api/v1/engines")
    assert resp.status_code == 200

    catalog = EnginesResponse.model_validate(resp.json())

    assert catalog.status == "ok"
    assert catalog.active
    assert catalog.engines
    assert any(engine.name == "qwen3_tts" for engine in catalog.engines)
