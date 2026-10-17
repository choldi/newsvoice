"""Tests del router multi-engine (locations + engine override)."""

from __future__ import annotations

import pytest

from tts_api.core.engine_factory import create_engine
from tts_api.core.engines.edgetts_engine import EdgeTTSEngine
from tts_api.core.engines.mock_engine import MockEngine
from tts_api.core.engines.routed_engine import RoutedEngine


def _routed() -> RoutedEngine:
    engines = {"mock": MockEngine(), "edgetts": EdgeTTSEngine()}
    routing = {"es": "mock", "ca": "edgetts", "_default": "mock"}
    return RoutedEngine(engines=engines, routing=routing)


def test_routed_capabilities_union_locations() -> None:
    caps = _routed().capabilities
    # mock runs locally, edgetts runs in the cloud; the union advertises both.
    assert set(caps.locations) == {"local", "cloud"}
    assert caps.stock_voices is True


def test_resolve_engine_picks_language_routing() -> None:
    r = _routed()
    assert isinstance(r._resolve_engine("ca"), EdgeTTSEngine)
    assert isinstance(r._resolve_engine("es"), MockEngine)


def test_resolve_engine_honours_explicit_override() -> None:
    """A pinned engine wins over the language routing."""
    r = _routed()
    # 'ca' would route to edgetts, but the explicit pin forces mock.
    assert isinstance(r._resolve_engine("ca", engine_name="mock"), MockEngine)
    # Comma-separated tags resolve to the first known engine.
    assert isinstance(r._resolve_engine("es", engine_name="nominate,edgetts"), EdgeTTSEngine)


async def test_routed_synthesize_forwards_engine_name() -> None:
    r = _routed()
    # 'ca' routes to edgetts (which raises FeatureNotSupportedError for cloning),
    # so pinning 'mock' must bypass the language routing.
    wav = await r.synthesize_clone(
        text="hola",
        reference_audio_path="/nope.wav",
        language="ca",
        engine_name="mock",
    )
    assert isinstance(wav, bytes) and len(wav) > 0


def test_factory_creates_qwen3() -> None:
    assert create_engine("qwen3_tts").name == "qwen3_tts"


def test_factory_creates_routed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTS_ENGINES", '{"es":"mock","ca":"edgetts","_default":"mock"}')
    assert create_engine("routed").name.startswith("routed(")
