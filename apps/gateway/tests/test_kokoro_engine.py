"""Tests unitarios del engine Kokoro (sin cargar el modelo real).

El test inyecta un ``KPipeline`` falso en vez de importar ``kokoro`` para no
descargar pesos ni arrastrar torch en CI.
"""

from __future__ import annotations

import io
import wave
from typing import ClassVar

import numpy as np
import pytest

from tts_api.core.engines.kokoro_engine import KokoroEngine
from tts_api.core.exceptions import (
    EngineUnavailableError,
    FeatureNotSupportedError,
    UnsupportedLanguageError,
    VoiceNotFoundError,
)


class _FakePipeline:
    """Minimal stand-in for kokoro.KPipeline."""

    instances: ClassVar[list[_FakePipeline]] = []

    def __init__(self, *, lang_code: str, repo_id: str) -> None:
        self.lang_code = lang_code
        self.repo_id = repo_id
        self.calls: list[tuple[str, str, float]] = []
        _FakePipeline.instances.append(self)

    def __call__(self, text, voice=None, speed=1.0, **_kwargs):
        self.calls.append((text, voice, speed))
        audio = np.zeros(2400, dtype=np.float32)
        yield "graphemes", "phonemes", audio


@pytest.fixture(autouse=True)
def fake_kokoro(monkeypatch: pytest.MonkeyPatch):
    """Install a fake ``kokoro`` module so KPipeline never downloads weights.

    Autouse: every test in this module gets the stub, so a stray real import
    can never reach the network. Tests that need to inspect the pipelines pass
    the fixture explicitly.
    """
    _FakePipeline.instances = []
    module = type("kokoro", (), {"KPipeline": _FakePipeline})
    monkeypatch.setitem(__import__("sys").modules, "kokoro", module)
    return _FakePipeline


def test_kokoro_engine_name():
    """El nombre del engine es estable para el router."""
    assert KokoroEngine().name == "kokoro"


def test_kokoro_engine_capabilities():
    """Kokoro declara voces stock y ni clonado ni instruct."""
    caps = KokoroEngine().capabilities
    assert caps.stock_voices
    assert not caps.voice_cloning
    assert not caps.instruct_voice_design
    assert not caps.emotions
    assert not caps.streaming
    assert "en" in caps.supported_languages
    assert "es" in caps.supported_languages


@pytest.mark.asyncio
async def test_kokoro_initialize_loads_default_language(fake_kokoro):
    """initialize() carga solo el pipeline del idioma por defecto (es -> e)."""
    engine = KokoroEngine()
    await engine.initialize()

    assert engine._pipelines
    assert list(engine._pipelines) == ["e"]
    assert fake_kokoro.instances[0].lang_code == "e"


@pytest.mark.asyncio
async def test_kokoro_synthesize_returns_pcm_wav():
    """synthesize() devuelve un WAV mono 24 kHz de 16 bits."""
    engine = KokoroEngine()
    result = await engine.synthesize(
        text="Hola mundo", voice_id="es-mx-male", language="es", speed=1.0
    )

    assert isinstance(result, bytes)
    with wave.open(io.BytesIO(result), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 24000
        assert wav_file.getnframes() == 2400


@pytest.mark.asyncio
async def test_kokoro_synthesize_passes_native_voice_and_speed():
    """La voz nativa y la velocidad llegan al pipeline."""
    engine = KokoroEngine()
    await engine.synthesize(text="Hola", voice_id="es-mx-male", language="es", speed=1.25)

    pipeline = engine._pipelines["e"]
    assert pipeline.calls == [("Hola", "em_alex", 1.25)]


@pytest.mark.asyncio
async def test_kokoro_synthesize_unknown_voice_raises():
    """Una voz desconocida no se traduce: VoiceNotFoundError."""
    engine = KokoroEngine()
    with pytest.raises(VoiceNotFoundError):
        await engine.synthesize(text="Hola", voice_id="nope", language="es")


@pytest.mark.asyncio
async def test_kokoro_unsupported_language_raises():
    """Un idioma sin lang_code (p.ej. catalán) se rechaza al cargar."""
    engine = KokoroEngine()
    with pytest.raises(UnsupportedLanguageError):
        await engine._ensure_pipeline("ca")


@pytest.mark.asyncio
async def test_kokoro_missing_dependency_raises(monkeypatch: pytest.MonkeyPatch):
    """Sin el paquete kokoro el engine avisa con EngineUnavailableError."""
    import sys

    monkeypatch.setitem(sys.modules, "kokoro", None)
    engine = KokoroEngine()
    with pytest.raises(EngineUnavailableError, match="kokoro"):
        await engine.initialize()


@pytest.mark.asyncio
async def test_kokoro_clone_and_instruct_are_rejected():
    """Clonado e instruct no están soportados."""
    engine = KokoroEngine()
    with pytest.raises(FeatureNotSupportedError):
        await engine.synthesize_clone(
            text="Hola", reference_audio_path="/fake/ref.wav", language="es"
        )
    with pytest.raises(FeatureNotSupportedError):
        await engine.synthesize_instruct(text="Hola", instruct="female", language="es")


@pytest.mark.asyncio
async def test_kokoro_list_stock_voices_filters_language():
    """El catálogo declara voces con idioma y género y filtra por idioma."""
    engine = KokoroEngine()
    voices = await engine.list_stock_voices()
    assert voices
    assert {"voice_id", "language", "gender", "name"} <= set(voices[0])

    es_voices = await engine.list_stock_voices(language="es")
    assert es_voices
    assert all(v["language"] == "es" for v in es_voices)


@pytest.mark.asyncio
async def test_kokoro_settings_override_voice_map(monkeypatch: pytest.MonkeyPatch):
    """KOKORO_VOICES redefine la voz nativa de un voice_id nuestro."""
    monkeypatch.setenv("KOKORO_VOICES", '{"es-mx-female": "ef_sofia"}')
    engine = KokoroEngine()
    table = engine._voice_table()
    assert table["es-mx-female"]["voice"] == "ef_sofia"


@pytest.mark.asyncio
async def test_kokoro_health_check():
    """El health reporta los idiomas cargados de forma perezosa."""
    engine = KokoroEngine()
    await engine.initialize()
    health = await engine.health_check()

    assert health["engine"] == "kokoro"
    assert health["mode"] == "REAL"
    assert health["loaded_languages"] == ["e"]
    assert health["stock_voices_count"] > 0


@pytest.mark.asyncio
async def test_kokoro_change_model_only_accepts_configured():
    """Kokoro tiene un solo modelo: solo el configurado es válido."""
    engine = KokoroEngine()
    assert await engine.change_model(engine._current_model)
    assert not await engine.change_model("kokoro-v0-1.0")
