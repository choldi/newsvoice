"""Tests unitarios del engine Chatterbox (sin cargar el modelo real)."""

from __future__ import annotations

import sys
import types
import wave
from io import BytesIO

import numpy as np
import pytest

from tts_api.core.engines.chatterbox_engine import ChatterboxEngine
from tts_api.core.exceptions import (
    EngineUnavailableError,
    InvalidReferenceAudioError,
    UnsupportedLanguageError,
    VoiceNotFoundError,
)

_DEFAULT_VOICE = "chatterbox-default"


class _FakeModel:
    """Modelo falso: devuelve una señal determinista sin GPU."""

    def __init__(self, sample_rate: int = 22050) -> None:
        self.sr = sample_rate
        self.calls: list[tuple[str, dict]] = []

    def generate(self, text: str, **kwargs) -> np.ndarray:
        self.calls.append((text, kwargs))
        return np.linspace(-0.4, 0.4, 2205, dtype=np.float32)


def _read_wav(data: bytes) -> dict:
    with wave.open(BytesIO(data), "rb") as wav_file:
        return {
            "channels": wav_file.getnchannels(),
            "sample_width": wav_file.getsampwidth(),
            "sample_rate": wav_file.getframerate(),
            "frames": wav_file.getnframes(),
        }


def test_chatterbox_engine_name():
    assert ChatterboxEngine().name == "chatterbox"


def test_chatterbox_engine_capabilities():
    caps = ChatterboxEngine().capabilities
    assert caps.stock_voices is True
    assert caps.voice_cloning is True
    assert caps.instruct_voice_design is True
    assert caps.emotions is False
    assert caps.streaming is False
    assert len(caps.supported_languages) == 23
    assert "es" in caps.supported_languages
    assert "en" in caps.supported_languages
    assert "ca" not in caps.supported_languages


async def test_chatterbox_list_stock_voices():
    engine = ChatterboxEngine()
    voices = await engine.list_stock_voices()
    assert len(voices) == 1
    assert {"voice_id", "language", "gender", "name"} <= set(voices[0])
    assert voices[0]["voice_id"] == _DEFAULT_VOICE


async def test_chatterbox_list_stock_voices_is_multilingual():
    engine = ChatterboxEngine()
    voices = await engine.list_stock_voices("es")
    assert voices[0]["language"] == "es"
    assert voices[0]["voice_id"] == _DEFAULT_VOICE


async def test_chatterbox_health_check_before_load():
    engine = ChatterboxEngine()
    health = await engine.health_check()
    assert health["model_loaded"] is False
    assert health["engine"] == "chatterbox"
    assert health["mode"] == "NOT_LOADED"
    assert health["stock_voices_count"] == 1


async def test_chatterbox_health_check_after_load():
    engine = ChatterboxEngine()
    engine._model = _FakeModel()
    health = await engine.health_check()
    assert health["model_loaded"] is True
    assert health["mode"] == "REAL"


async def test_chatterbox_initialize_does_not_load_model(
    monkeypatch: pytest.MonkeyPatch,
):
    fake = types.ModuleType("chatterbox")
    fake.mtl_tts = types.ModuleType("chatterbox.mtl_tts")
    monkeypatch.setitem(sys.modules, "chatterbox", fake)
    monkeypatch.setitem(sys.modules, "chatterbox.mtl_tts", fake.mtl_tts)

    engine = ChatterboxEngine()
    await engine.initialize()

    assert engine._ready is True
    assert engine._model is None


async def test_chatterbox_initialize_without_package(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "chatterbox", None)
    engine = ChatterboxEngine()
    with pytest.raises(EngineUnavailableError):
        await engine.initialize()
    assert engine._ready is False


async def test_chatterbox_synthesize_unknown_voice():
    engine = ChatterboxEngine()
    with pytest.raises(VoiceNotFoundError):
        await engine.synthesize(text="hola", voice_id="nope", language="es")


async def test_chatterbox_synthesize_unsupported_language():
    engine = ChatterboxEngine()
    with pytest.raises(UnsupportedLanguageError):
        await engine.synthesize(text="hola", voice_id=_DEFAULT_VOICE, language="ca")


async def test_chatterbox_synthesize_returns_valid_wav():
    engine = ChatterboxEngine()
    engine._model = _FakeModel()

    result = await engine.synthesize(
        text="Hola, això és una prova.", voice_id=_DEFAULT_VOICE, language="es"
    )
    assert isinstance(result, bytes)
    info = _read_wav(result)
    assert info["channels"] == 1
    assert info["sample_width"] == 2
    assert info["sample_rate"] == 22050
    assert info["frames"] == 2205


async def test_chatterbox_synthesize_passes_language_id():
    engine = ChatterboxEngine()
    fake = _FakeModel()
    engine._model = fake

    await engine.synthesize(text="hello", voice_id=_DEFAULT_VOICE, language="en")
    assert fake.calls[0][1]["language_id"] == "en"


async def test_chatterbox_synthesize_clone_missing_reference(tmp_path):
    engine = ChatterboxEngine()
    engine._model = _FakeModel()
    with pytest.raises(InvalidReferenceAudioError):
        await engine.synthesize_clone(
            text="hola", reference_audio_path=str(tmp_path / "missing.wav"), language="es"
        )


async def test_chatterbox_synthesize_clone_ok(tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"\x00")
    engine = ChatterboxEngine()
    fake = _FakeModel()
    engine._model = fake

    result = await engine.synthesize_clone(
        text="hola", reference_audio_path=str(ref), language="es"
    )
    assert _read_wav(result)["frames"] == 2205
    assert fake.calls[0][1]["audio_prompt_path"] == str(ref)


async def test_chatterbox_synthesize_instruct_maps_exaggeration_and_cfg():
    engine = ChatterboxEngine()
    fake = _FakeModel()
    engine._model = fake

    await engine.synthesize_instruct(
        text="hola", instruct="exaggeration=0.7 cfg=0.3", language="es"
    )
    kwargs = fake.calls[0][1]
    assert kwargs["exaggeration"] == pytest.approx(0.7)
    assert kwargs["cfg_weight"] == pytest.approx(0.3)


async def test_chatterbox_synthesize_instruct_uses_defaults_without_tokens():
    engine = ChatterboxEngine()
    fake = _FakeModel()
    engine._model = fake

    await engine.synthesize_instruct(text="hola", instruct="niña, joven", language="es")
    kwargs = fake.calls[0][1]
    assert kwargs["exaggeration"] == pytest.approx(0.5)
    assert kwargs["cfg_weight"] == pytest.approx(0.5)


def test_chatterbox_parse_instruct_clamps_ranges():
    assert ChatterboxEngine._parse_instruct("exaggeration=9.0") == pytest.approx((2.0, 0.5))
    assert ChatterboxEngine._parse_instruct("cfg=99") == pytest.approx((0.5, 1.0))
    assert ChatterboxEngine._parse_instruct("exaggeration=-1") == pytest.approx((0.0, 0.5))
    assert ChatterboxEngine._parse_instruct("cfg_weight=0.4 exaggeration=1.2") == pytest.approx(
        (1.2, 0.4)
    )
    assert ChatterboxEngine._parse_instruct("anything") == pytest.approx((0.5, 0.5))


async def test_chatterbox_close_releases_model():
    engine = ChatterboxEngine()
    engine._model = _FakeModel()
    await engine.close()
    assert engine._model is None
    assert engine._ready is False
