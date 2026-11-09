"""Tests for the engine factory."""

from __future__ import annotations

import pytest

from tts_api.core.engine_base import TtsEngineBase
from tts_api.core.engine_factory import (
    _create_routed_engine,
    _create_single_engine,
    create_engine,
)


def test_create_single_engine_mock():
    """Test creating a mock engine."""
    engine = _create_single_engine("mock")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "mock"


def test_create_single_engine_pocket_tts():
    """Test creating a pocket_tts engine."""
    engine = _create_single_engine("pocket_tts")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "pocket_tts"


def test_create_single_engine_edgetts():
    """Test creating an edgetts engine."""
    engine = _create_single_engine("edgetts")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "edgetts"


def test_create_single_engine_omnivoice():
    """Test creating an omnivoice engine."""
    engine = _create_single_engine("omnivoice")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "omnivoice"


def test_create_single_engine_omnivoice_remote():
    """Test creating the HTTP proxy engine for the OmniVoice sidecar."""
    engine = _create_single_engine("omnivoice_remote")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "omnivoice_remote"


def test_create_single_engine_qwen3_uses_the_remote_sidecar(monkeypatch):
    """A configured remote URL switches qwen3_tts to the HTTP proxy."""
    monkeypatch.setenv("QWEN3_TTS_REMOTE_URL", "http://gateway-qwen3:8000/api/v1")

    engine = _create_single_engine("qwen3_tts")

    assert engine.name == "qwen3_tts"
    assert engine.__class__.__name__ == "Qwen3RemoteEngine"


def test_create_single_engine_qwen3_defaults_to_the_local_worker(monkeypatch):
    """Without a remote URL the engine keeps the external-venv worker path."""
    monkeypatch.setenv("QWEN3_TTS_REMOTE_URL", "")

    engine = _create_single_engine("qwen3_tts")

    assert engine.name == "qwen3_tts"
    assert engine.__class__.__name__ == "Qwen3TTSEngine"


def test_create_single_engine_zono2():
    """Test creating a zono2 engine."""
    engine = _create_single_engine("zono2")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "zono2"


def test_create_single_engine_kokoro():
    """Test creating a kokoro engine."""
    engine = _create_single_engine("kokoro")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "kokoro"


def test_create_single_engine_piper():
    """Test creating a piper engine."""
    engine = _create_single_engine("piper")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "piper"


def test_create_single_engine_chatterbox():
    """Test creating a chatterbox engine."""
    engine = _create_single_engine("chatterbox")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "chatterbox"


def test_create_single_engine_fish_tts():
    """Fish Audio is instantiable even before credentials are validated."""
    engine = _create_single_engine("fish_tts")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "fish_tts"


def test_create_single_engine_openai_tts():
    """The OpenAI-compatible provider is instantiable from the factory."""
    engine = _create_single_engine("openai_tts")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "openai_tts"


def test_create_single_engine_unknown():
    """Test creating an unknown engine raises ValueError."""
    with pytest.raises(ValueError, match="Engine desconocido"):
        _create_single_engine("unknown_engine")


def test_create_single_engine_case_insensitive():
    """Test that engine names are case insensitive."""
    engine1 = _create_single_engine("ZONO2")
    engine2 = _create_single_engine("zono2")
    assert engine1.name == engine2.name == "zono2"


def test_create_engine_with_zono2_param():
    """Test creating engine with explicit parameter."""
    engine = create_engine("zono2")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "zono2"


def test_create_engine_with_kokoro_param():
    """Test creating engine with explicit parameter."""
    engine = create_engine("kokoro")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "kokoro"


def test_create_engine_with_piper_param():
    """Test creating engine with explicit parameter."""
    engine = create_engine("piper")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "piper"


def test_create_engine_with_chatterbox_param():
    """Test creating engine with explicit parameter."""
    engine = create_engine("chatterbox")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "chatterbox"


def test_create_routed_engine_with_extra_engines():
    """Extra engines are instantiated without routing any language to them."""
    engine = _create_routed_engine(
        '{"es":"mock","_default":"mock"}', extra_engines="omnivoice_remote"
    )
    assert engine.name.startswith("routed(")
    assert set(engine._engines) == {"mock", "omnivoice_remote"}


def test_routed_adds_configured_cloud_engines(monkeypatch):
    """Configured cloud engines join the routed union as optional engines."""
    monkeypatch.setenv("FISH_API_KEY", "fish-key")
    monkeypatch.setenv("OPENAI_TTS_BASE_URL", "https://tts.example/v1")
    engine = _create_routed_engine('{"es":"mock","_default":"mock"}', extra_engines="")

    assert {"fish_tts", "openai_tts"} <= set(engine._engines)
    assert engine._optional_engines == {"fish_tts", "openai_tts"}


def test_routed_skips_unconfigured_cloud_engines(monkeypatch):
    """An empty key/base URL keeps cloud engines out of the local stack."""
    monkeypatch.setenv("FISH_API_KEY", "")
    monkeypatch.setenv("OPENAI_TTS_BASE_URL", "")
    engine = _create_routed_engine('{"es":"mock","_default":"mock"}', extra_engines="")

    assert "fish_tts" not in engine._engines
    assert "openai_tts" not in engine._engines


def test_routed_adds_qwen3_remote_as_optional(monkeypatch):
    """A configured sidecar joins the union but does not gate routed health."""
    monkeypatch.setenv("FISH_API_KEY", "")
    monkeypatch.setenv("OPENAI_TTS_BASE_URL", "")
    monkeypatch.setenv("QWEN3_TTS_REMOTE_URL", "http://gateway-qwen3:8000/api/v1")

    engine = _create_routed_engine('{"es":"mock","_default":"mock"}', extra_engines="")

    assert "qwen3_tts" in engine._engines
    assert engine._optional_engines == {"qwen3_tts"}


def test_routed_keeps_qwen3_remote_required_when_routed(monkeypatch):
    """A language explicitly routed to qwen3_tts makes it a required engine."""
    monkeypatch.setenv("FISH_API_KEY", "")
    monkeypatch.setenv("OPENAI_TTS_BASE_URL", "")
    monkeypatch.setenv("QWEN3_TTS_REMOTE_URL", "http://gateway-qwen3:8000/api/v1")

    engine = _create_routed_engine('{"es":"qwen3_tts","_default":"mock"}', extra_engines="")

    assert "qwen3_tts" in engine._engines
    assert "qwen3_tts" not in engine._optional_engines


def test_create_engine_reads_extra_engines(monkeypatch):
    """create_engine wires TTS_EXTRA_ENGINES into the routed engine."""
    monkeypatch.setenv("TTS_ENGINE", "routed")
    monkeypatch.setenv("TTS_ENGINES", '{"es":"mock","_default":"mock"}')
    monkeypatch.setenv("TTS_EXTRA_ENGINES", "omnivoice_remote")
    engine = create_engine()
    assert "omnivoice_remote" in engine._engines
