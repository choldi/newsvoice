"""Tests for the declarative TTS engine registry."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tts_api.core.engine_registry import (
    EngineCapabilitySpec,
    EngineKind,
    EngineRegistry,
    EngineSpecPatch,
    known_engine_names,
    registry_for,
)
from tts_api.settings import Settings

_MANIFEST = """\
version = 1

[engines.mock]
kind = "local_class"
class_path = "tts_api.core.engines.mock_engine.MockEngine"
display_name = "Mock"

[engines.edgetts]
kind = "local_class"
class_path = "tts_api.core.engines.edgetts_engine.EdgeTTSEngine"
"""


def _registry_files(tmp_path: Path, override: dict | None = None) -> tuple[Path, Path]:
    base = tmp_path / "engines.toml"
    override_path = tmp_path / "engines.local.json"
    base.write_text(_MANIFEST, encoding="utf-8")
    override_path.write_text(json.dumps(override or {}), encoding="utf-8")
    return base, override_path


def test_registry_loads_manifest(tmp_path: Path) -> None:
    base, override_path = _registry_files(tmp_path)
    registry = EngineRegistry(base, override_path)

    assert registry.names() == ["edgetts", "mock"]
    assert registry.get("mock").display_name == "Mock"
    assert registry.active is None


def test_registry_merges_override_capabilities(tmp_path: Path) -> None:
    override = {
        "version": 1,
        "active": "mock",
        "engines": {
            "mock": {"capabilities": {"supported_languages": ["es"]}},
        },
    }
    base, override_path = _registry_files(tmp_path, override)
    registry = EngineRegistry(base, override_path)

    spec = registry.get("mock")
    assert spec.capabilities.supported_languages == ["es"]
    assert spec.capabilities.stock_voices is True
    assert registry.active == "mock"
    assert registry.active_name("edgetts") == "mock"


def test_registry_upsert_saves_a_new_provider(tmp_path: Path) -> None:
    base, override_path = _registry_files(tmp_path)
    registry = EngineRegistry(base, override_path)
    patch = EngineSpecPatch(
        kind=EngineKind.OPENAI_COMPATIBLE_HTTP,
        display_name="Vendor TTS",
        locations=["cloud"],
        capabilities=EngineCapabilitySpec(supported_languages=["en"]),
        options={
            "base_url_env": "VENDOR_BASE_URL",
            "voices": {"vendor-en": {"voice": "Voice 1", "language": "en"}},
        },
    )

    spec = registry.upsert("VendorTTS", patch)
    registry.save()

    assert spec.name == "vendortts"
    assert spec.kind is EngineKind.OPENAI_COMPATIBLE_HTTP
    stored = json.loads(override_path.read_text(encoding="utf-8"))
    assert stored["engines"]["vendortts"]["display_name"] == "Vendor TTS"
    assert EngineRegistry(base, override_path).get("vendortts").name == "vendortts"


def test_registry_validate_reports_missing_class_path(tmp_path: Path) -> None:
    base, override_path = _registry_files(tmp_path)
    registry = EngineRegistry(base, override_path)

    errors = registry.validate("broken", EngineSpecPatch())

    assert any("class_path" in error for error in errors)
    assert registry.names() == ["edgetts", "mock"]


def test_registry_remove_disables_manifest_engine_and_deletes_custom(tmp_path: Path) -> None:
    override = {"version": 1, "active": "custom", "engines": {"custom": {}}}
    base, override_path = _registry_files(tmp_path, override)
    registry = EngineRegistry(base, override_path)
    registry._specs["custom"] = registry.candidate(
        "custom",
        EngineSpecPatch(
            kind=EngineKind.OPENAI_COMPATIBLE_HTTP,
            options={"base_url_env": "CUSTOM_BASE_URL"},
        ),
    )

    assert registry.remove("mock") is True
    assert registry.get("mock").enabled is False
    assert registry.remove("custom") is True
    assert "custom" not in registry.names()
    assert registry.active is None


def test_set_active_rejects_disabled_engine(tmp_path: Path) -> None:
    base, override_path = _registry_files(tmp_path)
    registry = EngineRegistry(base, override_path)
    registry.upsert("mock", EngineSpecPatch(enabled=False))

    with pytest.raises(ValueError, match="desactivado"):
        registry.set_active("mock")


def test_default_manifest_includes_builtin_engines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TTS_ENGINE_REGISTRY_PATH", raising=False)
    monkeypatch.delenv("TTS_ENGINE_OVERRIDE_PATH", raising=False)

    assert {"omnivoice", "pocket_tts", "openai_tts"}.issubset(known_engine_names())


def test_load_uses_environment_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base, override_path = _registry_files(tmp_path)
    monkeypatch.setenv("TTS_ENGINE_REGISTRY_PATH", str(base))
    monkeypatch.setenv("TTS_ENGINE_OVERRIDE_PATH", str(override_path))

    assert known_engine_names() == ["edgetts", "mock"]


def test_registry_for_uses_settings_paths(tmp_path: Path) -> None:
    base, override_path = _registry_files(tmp_path)
    settings = SimpleNamespace(
        TTS_ENGINE_REGISTRY_PATH=base,
        TTS_ENGINE_OVERRIDE_PATH=override_path,
    )

    assert registry_for(settings).names() == ["edgetts", "mock"]


def test_settings_effective_engine_prefers_staged_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, override_path = _registry_files(tmp_path, {"version": 1, "active": "mock", "engines": {}})
    monkeypatch.setenv("TTS_ENGINE_REGISTRY_PATH", str(base))
    monkeypatch.setenv("TTS_ENGINE_OVERRIDE_PATH", str(override_path))
    monkeypatch.setenv("TTS_ENGINE", "edgetts")
    settings = Settings()

    assert settings.effective_tts_engine == "mock"


def test_gateway_dockerfiles_copy_engine_manifest() -> None:
    repo_root = Path(__file__).resolve().parents[3]

    for name in ("Dockerfile", "Dockerfile.qwen3", "Dockerfile.omnivoice"):
        content = (repo_root / "apps/gateway" / name).read_text(encoding="utf-8")
        assert "COPY config/engines.toml ./config/engines.toml" in content
