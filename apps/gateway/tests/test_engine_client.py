"""Tests unitarios para el cliente del engine (multi-engine)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tts_api.core.engine_client import (
    AudioResult,
    OmniVoiceEngineClient,
)
from tts_api.core.exceptions import (
    UnsupportedInstructError,
)


def _make_wav_bytes(sample_rate: int = 22050, duration_sec: float = 1.0) -> bytes:
    """Genera bytes WAV válidos para tests."""
    import io
    import wave

    num_samples = int(duration_sec * sample_rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * num_samples)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_start_and_stop() -> None:
    """El cliente inicializa/cierra el engine inyectado al arrancar/parar."""
    mock_engine = AsyncMock()
    client = OmniVoiceEngineClient(engine=mock_engine)

    await client.start()
    assert client._started is True
    assert client.engine is mock_engine
    mock_engine.initialize.assert_awaited_once()

    await client.stop()
    assert client._started is False
    assert client.engine is None
    mock_engine.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_synthesize_stock() -> None:
    """synthesize_stock delega en engine.synthesize() y envuelve el WAV."""
    wav = _make_wav_bytes()
    mock_engine = AsyncMock()
    mock_engine.synthesize.return_value = wav
    client = OmniVoiceEngineClient(engine=mock_engine)
    await client.start()

    result = await client.synthesize_stock(text="Hola", voice_id="es-mx-male")
    assert isinstance(result, AudioResult)
    assert result.wav_bytes == wav
    mock_engine.synthesize.assert_awaited_once_with(
        text="Hola",
        voice_id="es-mx-male",
        language="es",
        speed=1.0,
        emotion=None,
    )


@pytest.mark.asyncio
async def test_synthesize_stock_passes_params() -> None:
    """Los parámetros speed/emotion/language se propagan al engine."""
    wav = _make_wav_bytes()
    mock_engine = AsyncMock()
    mock_engine.synthesize.return_value = wav
    client = OmniVoiceEngineClient(engine=mock_engine)
    await client.start()

    result = await client.synthesize_stock(
        text="Hola",
        voice_id="es-mx-male",
        speed=0.9,
        emotion="happy",
        language="es",
    )
    assert isinstance(result, AudioResult)
    mock_engine.synthesize.assert_awaited_once_with(
        text="Hola",
        voice_id="es-mx-male",
        language="es",
        speed=0.9,
        emotion="happy",
    )


@pytest.mark.asyncio
async def test_synthesize_instruct() -> None:
    """synthesize_instruct delega en engine.synthesize_instruct()."""
    wav = _make_wav_bytes()
    mock_engine = AsyncMock()
    mock_engine.synthesize_instruct.return_value = wav
    client = OmniVoiceEngineClient(engine=mock_engine)
    await client.start()

    result = await client.synthesize_instruct(
        text="Hello",
        instruct="female, british accent",
        language="en",
    )
    assert isinstance(result, AudioResult)
    assert result.wav_bytes == wav
    mock_engine.synthesize_instruct.assert_awaited_once_with(
        text="Hello",
        instruct="female, british accent",
        language="en",
        speed=1.0,
        emotion=None,
    )


@pytest.mark.asyncio
async def test_synthesize_instruct_propagates_invalid_instruct_error() -> None:
    """Los errores del engine se propagan sin traducir (p.ej. UX del router)."""
    mock_engine = AsyncMock()
    mock_engine.synthesize_instruct.side_effect = UnsupportedInstructError(
        "Mexican accent", {"mexican accent": None}, ["male", "female"]
    )
    client = OmniVoiceEngineClient(engine=mock_engine)
    await client.start()

    with pytest.raises(UnsupportedInstructError):
        await client.synthesize_instruct(
            text="Hello",
            instruct="Mexican accent",
        )


@pytest.mark.asyncio
async def test_synthesize_clone() -> None:
    """synthesize_clone delega en engine.synthesize_clone() con ref_text."""
    wav = _make_wav_bytes()
    mock_engine = AsyncMock()
    mock_engine.synthesize_clone.return_value = wav
    client = OmniVoiceEngineClient(engine=mock_engine)
    await client.start()

    result = await client.synthesize_clone(
        text="Hola",
        reference_audio_path="/path/to/ref.wav",
        language="es",
    )
    assert isinstance(result, AudioResult)
    assert result.wav_bytes == wav
    mock_engine.synthesize_clone.assert_awaited_once_with(
        text="Hola",
        reference_audio_path="/path/to/ref.wav",
        language="es",
        speed=1.0,
        ref_text=None,
    )


@pytest.mark.asyncio
async def test_synthesize_clone_with_ref_text() -> None:
    """ref_text (transcripción precomputada) se propaga al engine."""
    wav = _make_wav_bytes()
    mock_engine = AsyncMock()
    mock_engine.synthesize_clone.return_value = wav
    client = OmniVoiceEngineClient(engine=mock_engine)
    await client.start()

    result = await client.synthesize_clone(
        text="Hola",
        reference_audio_path="/path/to/ref.wav",
        ref_text="Hola, esta es la transcripción",
        language="es",
    )
    assert isinstance(result, AudioResult)
    mock_engine.synthesize_clone.assert_awaited_once_with(
        text="Hola",
        reference_audio_path="/path/to/ref.wav",
        language="es",
        speed=1.0,
        ref_text="Hola, esta es la transcripción",
    )


@pytest.mark.asyncio
async def test_health() -> None:
    """health() mapea health_check() del engine a EngineHealth."""
    mock_engine = AsyncMock()
    mock_engine.health_check.return_value = {
        "model_loaded": True,
        "gpu_available": True,
        "device": "cuda:0",
        "stock_voices_count": 10,
    }
    client = OmniVoiceEngineClient(engine=mock_engine)
    await client.start()

    health = await client.health()
    assert health.model_loaded is True
    assert health.gpu_available is True


def test_shared_engine_client_es_un_singleton() -> None:
    """get_shared_engine_client reutiliza una única instancia por proceso."""
    from tts_api.core.engine_client import (
        get_shared_engine_client,
        reset_shared_engine_client,
        set_shared_engine_client,
    )

    reset_shared_engine_client()
    try:
        first = get_shared_engine_client()
        assert get_shared_engine_client() is first

        engine = AsyncMock()
        installed = set_shared_engine_client(engine)

        assert installed is get_shared_engine_client()
        assert installed is not first
        assert installed.engine is engine
    finally:
        reset_shared_engine_client()


def test_reset_shared_engine_client_crea_otra_instancia() -> None:
    """reset_shared_engine_client descarta el cliente compartido."""
    from tts_api.core.engine_client import (
        get_shared_engine_client,
        reset_shared_engine_client,
    )

    reset_shared_engine_client()
    try:
        first = get_shared_engine_client()
        reset_shared_engine_client()

        assert get_shared_engine_client() is not first
    finally:
        reset_shared_engine_client()
