"""Tests unitarios del engine Piper (sin descargar el modelo real)."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from tts_api.core.engines.piper_engine import PiperEngine
from tts_api.core.exceptions import (
    EngineUnavailableError,
    FeatureNotSupportedError,
    UnsupportedLanguageError,
    VoiceNotFoundError,
)

FAKE_WAV = (
    b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
    b"\x01\x00\x01\x00\x44\xac\x00\x00\x88\x58\x01\x00"
    b"\x02\x00\x10\x00data\x00\x00\x00\x00"
)


def _stub_render(*_args: Any, **_kwargs: Any) -> bytes:
    return FAKE_WAV


def test_piper_engine_name():
    engine = PiperEngine()
    assert engine.name == "piper"


def test_piper_engine_capabilities():
    caps = PiperEngine().capabilities
    assert caps.stock_voices is True
    assert caps.voice_cloning is False
    assert caps.instruct_voice_design is False
    assert caps.emotions is False
    assert caps.streaming is False
    for lang in ("ca", "es", "en", "fr"):
        assert lang in caps.supported_languages


async def test_piper_exposes_catalan_voices():
    engine = PiperEngine()
    assert engine.capabilities.supported_languages.count("ca") == 1
    voices = await engine.list_stock_voices()
    assert "ca-female" in [v["voice_id"] for v in voices]


async def test_piper_list_stock_voices():
    engine = PiperEngine()
    voices = await engine.list_stock_voices()
    assert isinstance(voices, list)
    assert len(voices) >= 12
    first = voices[0]
    assert {"voice_id", "language", "gender", "name"} <= set(first)


async def test_piper_list_stock_voices_filtered():
    engine = PiperEngine()
    ca = await engine.list_stock_voices("ca")
    assert ca and all(v["language"] == "ca" for v in ca)

    fr = await engine.list_stock_voices(language="fr")
    assert fr and all(v["language"] == "fr" for v in fr)

    de = await engine.list_stock_voices("de")
    assert de == []


async def test_piper_health_check_not_loaded():
    engine = PiperEngine()
    health = await engine.health_check()
    assert health["model_loaded"] is False
    assert health["engine"] == "piper"
    assert health["mode"] == "NOT_LOADED"
    assert health["stock_voices_count"] >= 12


async def test_piper_synthesize_unknown_voice():
    engine = PiperEngine()
    with pytest.raises(VoiceNotFoundError) as exc_info:
        await engine.synthesize(text="hola", voice_id="nope", language="es")
    assert exc_info.value.valid_voice_ids is not None


async def test_piper_synthesize_unsupported_language():
    engine = PiperEngine()
    with pytest.raises(UnsupportedLanguageError):
        await engine.synthesize(text="hola", voice_id="ca-female", language="xx")


async def test_piper_synthesize_clone_not_supported():
    engine = PiperEngine()
    with pytest.raises(FeatureNotSupportedError):
        await engine.synthesize_clone(
            text="hola", reference_audio_path="/fake/ref.wav", language="es"
        )


async def test_piper_synthesize_instruct_not_supported():
    engine = PiperEngine()
    with pytest.raises(FeatureNotSupportedError):
        await engine.synthesize_instruct(text="hola", instruct="niña", language="es")


async def test_piper_synthesize_returns_wav(monkeypatch: pytest.MonkeyPatch):
    engine = PiperEngine()
    monkeypatch.setattr(engine, "_load_voice", lambda name: f"voice:{name}")
    monkeypatch.setattr(engine, "_render", _stub_render)

    result = await engine.synthesize(
        text="Hola, això és una prova.", voice_id="ca-female", language="ca", speed=1.25
    )
    assert isinstance(result, bytes)
    assert result == FAKE_WAV


async def test_piper_initialize_without_package(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "piper", None)
    engine = PiperEngine()
    with pytest.raises(EngineUnavailableError):
        await engine.initialize()
    assert engine._ready is False


async def test_piper_initialize_is_idempotent(monkeypatch: pytest.MonkeyPatch):
    fake_piper = types.ModuleType("piper")
    monkeypatch.setitem(sys.modules, "piper", fake_piper)
    engine = PiperEngine()
    monkeypatch.setattr(engine, "_load_voice", lambda _name: object())

    await engine.initialize()
    assert engine._ready is True
    await engine.initialize()
    assert engine._ready is True


async def test_piper_initialize_tolerates_missing_default_voice(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_piper = types.ModuleType("piper")
    monkeypatch.setitem(sys.modules, "piper", fake_piper)

    def _boom(_name: str) -> object:
        raise EngineUnavailableError("falta la voz")

    engine = PiperEngine()
    monkeypatch.setattr(engine, "_load_voice", _boom)

    await engine.initialize()
    assert engine._ready is True


@pytest.mark.parametrize(
    ("speed", "expected"),
    [
        (1.0, 1.0),
        (2.0, 0.5),
        (0.5, 2.0),
        (0.0, 1.0),
        (-1.0, 1.0),
        (8.0, 0.25),
        (0.1, 4.0),
    ],
)
def test_piper_length_scale(speed: float, expected: float):
    assert PiperEngine._length_scale(speed) == pytest.approx(expected)


def test_piper_load_voice_missing_model_no_download(tmp_path):
    engine = PiperEngine()
    engine._voices_dir = tmp_path
    engine._auto_download = False
    with pytest.raises(EngineUnavailableError):
        engine._load_voice("no-such-voice")


def test_piper_load_voice_download_but_still_missing(tmp_path, monkeypatch: pytest.MonkeyPatch):
    engine = PiperEngine()
    engine._voices_dir = tmp_path
    engine._auto_download = True
    monkeypatch.setattr(engine, "_download_voice", lambda _name: None)

    with pytest.raises(EngineUnavailableError):
        engine._load_voice("no-such-voice")


async def test_piper_close_clears_cache(monkeypatch: pytest.MonkeyPatch):
    fake_piper = types.ModuleType("piper")
    monkeypatch.setitem(sys.modules, "piper", fake_piper)
    engine = PiperEngine()
    monkeypatch.setattr(engine, "_load_voice", lambda _name: object())
    await engine.initialize()

    engine._voices["x"] = object()
    await engine.close()
    assert engine._voices == {}
    assert engine._ready is False
