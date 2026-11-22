"""Tests for the declarative engine admin API."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tts_api.api.v1 import admin_engines
from tts_api.core.engine_registry import EngineRegistry
from tts_api.main import app

_MANIFEST = """\
version = 1

[engines.mock]
kind = "local_class"
class_path = "tts_api.core.engines.mock_engine.MockEngine"
display_name = "Mock"
"""


@pytest.fixture
def registry(tmp_path: Path) -> EngineRegistry:
    base = tmp_path / "engines.toml"
    override = tmp_path / "engines.local.json"
    base.write_text(_MANIFEST, encoding="utf-8")
    override.write_text("{}", encoding="utf-8")
    return EngineRegistry(base, override)


@pytest.fixture
def client(registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    def fake_settings() -> SimpleNamespace:
        return SimpleNamespace(
            engine_registry=registry,
            effective_tts_engine=registry.active_name("mock"),
        )

    monkeypatch.setattr(admin_engines, "get_settings", fake_settings)
    return TestClient(app)


def test_list_registry_returns_effective_specs(client: TestClient) -> None:
    response = client.get("/api/v1/admin/engines")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["active"] == "mock"
    assert [spec["name"] for spec in body["engines"]] == ["mock"]


def test_validate_reports_schema_errors_without_writing(
    client: TestClient, registry: EngineRegistry
) -> None:
    response = client.post(
        "/api/v1/admin/engines/validate",
        json={"id": "broken", "spec": {"kind": "local_class"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["restart_required"] is False
    assert any("class_path" in error for error in body["errors"])
    assert json.loads(registry.override_path.read_text(encoding="utf-8")) == {}


def test_upsert_stages_a_provider_and_requires_restart(
    client: TestClient, registry: EngineRegistry
) -> None:
    response = client.put(
        "/api/v1/admin/engines/vendor",
        json={
            "kind": "openai_compatible_http",
            "display_name": "Vendor TTS",
            "locations": ["cloud"],
            "options": {"base_url_env": "VENDOR_BASE_URL"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["engine"] == "vendor"
    assert body["restart_required"] is True
    assert body["spec"]["kind"] == "openai_compatible_http"
    stored = json.loads(registry.override_path.read_text(encoding="utf-8"))
    assert "vendor" in stored["engines"]


def test_activate_stages_the_next_restart(client: TestClient, registry: EngineRegistry) -> None:
    client.put(
        "/api/v1/admin/engines/vendor",
        json={
            "kind": "openai_compatible_http",
            "options": {"base_url_env": "VENDOR_BASE_URL"},
        },
    )
    response = client.post("/api/v1/admin/engines/activate", json={"id": "vendor"})

    assert response.status_code == 200
    assert response.json()["restart_required"] is True
    assert registry.active == "vendor"
    assert client.get("/api/v1/admin/engines").json()["active"] == "vendor"


def test_delete_disables_a_manifest_engine(client: TestClient, registry: EngineRegistry) -> None:
    response = client.delete("/api/v1/admin/engines/mock")

    assert response.status_code == 200
    assert registry.get("mock").enabled is False


def test_activate_unknown_engine_returns_400(client: TestClient) -> None:
    response = client.post("/api/v1/admin/engines/activate", json={"id": "missing"})

    assert response.status_code == 400
    assert response.json()["detail"]["error_type"] == "engine_registry_error"
