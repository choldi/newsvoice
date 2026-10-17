"""Tests unitarios del engine Kokoro (sin cargar el modelo real)."""

from __future__ import annotations

import io
import wave

import pytest

from tts_api.core.engines.kokoro_engine import KokoroEngine
from tts_api.core.exceptions import FeatureNotSupportedError, UnsupportedLanguageError


def test_kokoro_engine_name():
    """Test that the engine name is correct."""
    engine = KokoroEngine()
    assert engine.name == "kokoro"


def test_kokoro_engine_capabilities():
    """Test that the engine capabilities are correct."""
    engine = KokoroEngine()
    caps = engine.capabilities
    assert caps.stock_voices
    assert not caps.voice_cloning
    assert not caps.instruct_voice_design
    assert not caps.emotions
    assert not caps.streaming
    assert "en" in caps.supported_languages
    assert "es" in caps.supported_languages


@pytest.mark.asyncio
async def test_kokoro_engine_initialize():
    """Test that the engine initializes correctly."""
    engine = KokoroEngine()
    assert not engine._model_loaded

    await engine.initialize()
    assert engine._model_loaded

    # Calling initialize again should not change anything
    await engine.initialize()
    assert engine._model_loaded


@pytest.mark.asyncio
async def test_kokoro_engine_synthesize():
    """Test that the engine can synthesize text."""
    engine = KokoroEngine()
    await engine.initialize()

    # Test with English text
    result = await engine.synthesize(
        text="Hello world", voice_id="kokoro-male-1", language="en", speed=1.0
    )

    # Should return WAV bytes
    assert isinstance(result, bytes)
    assert len(result) > 0

    # Verify it's a valid WAV file
    buffer = io.BytesIO(result)
    with wave.open(buffer, "rb") as wav_file:
        assert wav_file.getnchannels() == 1  # Mono
        assert wav_file.getsampwidth() == 2  # 16-bit
        assert wav_file.getframerate() == 24000


@pytest.mark.asyncio
async def test_kokoro_engine_synthesize_unsupported_language():
    """Test that the engine rejects unsupported languages."""
    engine = KokoroEngine()
    await engine.initialize()

    with pytest.raises(UnsupportedLanguageError):
        await engine.synthesize(
            text="Hello world",
            voice_id="kokoro-male-1",
            language="xx",  # Unsupported language
            speed=1.0,
        )


@pytest.mark.asyncio
async def test_kokoro_engine_synthesize_clone():
    """Test that the engine raises FeatureNotSupportedError for cloning."""
    engine = KokoroEngine()
    await engine.initialize()

    with pytest.raises(FeatureNotSupportedError):
        await engine.synthesize_clone(
            text="Hello world",
            reference_audio_path="/fake/path/reference.wav",
            language="en",
            speed=1.0,
        )


@pytest.mark.asyncio
async def test_kokoro_engine_synthesize_instruct():
    """Test that the engine raises FeatureNotSupportedError for instruct."""
    engine = KokoroEngine()
    await engine.initialize()

    with pytest.raises(FeatureNotSupportedError):
        await engine.synthesize_instruct(
            text="Hello world", instruct="female, young adult", language="en", speed=1.0
        )


@pytest.mark.asyncio
async def test_kokoro_engine_list_stock_voices():
    """Test that the engine can list stock voices."""
    engine = KokoroEngine()
    voices = await engine.list_stock_voices()

    assert isinstance(voices, list)
    assert len(voices) > 0

    # Check structure of first voice
    voice = voices[0]
    assert "voice_id" in voice
    assert "language" in voice
    assert "gender" in voice
    assert "name" in voice

    # Test filtering by language
    en_voices = await engine.list_stock_voices(language="en")
    assert all(v["language"] == "en" for v in en_voices)

    es_voices = await engine.list_stock_voices(language="es")
    assert all(v["language"] == "es" for v in es_voices)


@pytest.mark.asyncio
async def test_kokoro_engine_health_check():
    """Test that the engine health check works."""
    engine = KokoroEngine()
    health = await engine.health_check()

    assert isinstance(health, dict)
    assert "model_loaded" in health
    assert "engine" in health
    assert health["engine"] == "kokoro"
    assert "stock_voices_count" in health


@pytest.mark.asyncio
async def test_kokoro_engine_list_available_models():
    """Test that the engine can list available models."""
    engine = KokoroEngine()
    models = await engine.list_available_models()

    assert isinstance(models, list)
    assert len(models) > 0
    assert "kokoro-v0-1.0" in models
    assert "kokoro-v0-1.1" in models
    assert "kokoro-v1-0" in models


@pytest.mark.asyncio
async def test_kokoro_engine_list_available_features():
    """Test that the engine can list available features."""
    engine = KokoroEngine()
    features = await engine.list_available_features()

    assert isinstance(features, dict)
    assert "stock_voices" in features
    assert "voice_cloning" in features
    assert "instruct_voice_design" in features
    assert "emotions" in features
    assert "streaming" in features
    assert "supported_languages" in features
    assert "available_models" in features
    assert "current_model" in features


@pytest.mark.asyncio
async def test_kokoro_engine_change_model():
    """Test that the engine can change models."""
    engine = KokoroEngine()

    # Test changing to a valid model
    result = await engine.change_model("kokoro-v0-1.1")
    assert result
    assert engine._current_model == "kokoro-v0-1.1"

    # Test changing to the same model (should return True)
    result = await engine.change_model("kokoro-v0-1.1")
    assert result

    # Test changing to an invalid model (should return False)
    result = await engine.change_model("invalid-model")
    assert not result
    assert engine._current_model == "kokoro-v0-1.1"  # Should remain unchanged
