"""Tests unitarios para TtsService."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tts_api.core.engine_client import AudioResult, OmniVoiceEngineClient
from tts_api.core.exceptions import (
    UnsupportedLanguageError,
    VoiceNotFoundError,
)
from tts_api.services.tts import TtsService


@pytest.fixture
def tts_service() -> TtsService:
    return TtsService()


@pytest.mark.asyncio
async def test_synthesize_stock_success(tts_service: TtsService) -> None:
    mock_client = AsyncMock(spec=OmniVoiceEngineClient)
    mock_client.list_stock_voices = AsyncMock(
        return_value=[
            MagicMock(voice_id="es-mx-male"),
        ]
    )
    mock_client.synthesize_stock = AsyncMock(
        return_value=AudioResult(
            wav_bytes=b"RIFF" + b"\x00" * 100,
            duration_sec=1.0,
            sample_rate=22050,
        )
    )
    tts_service._engine_client = mock_client

    result = await tts_service.synthesize_stock(
        text="Hola",
        voice_id="es-mx-male",
        language="es",
    )
    assert isinstance(result, AudioResult)


@pytest.mark.asyncio
async def test_synthesize_stock_passes_params(tts_service: TtsService) -> None:
    mock_client = AsyncMock(spec=OmniVoiceEngineClient)
    mock_client.list_stock_voices = AsyncMock(
        return_value=[
            MagicMock(voice_id="es-mx-male"),
        ]
    )
    mock_client.synthesize_stock = AsyncMock(
        return_value=AudioResult(
            wav_bytes=b"RIFF" + b"\x00" * 100,
            duration_sec=1.0,
            sample_rate=22050,
        )
    )
    tts_service._engine_client = mock_client

    result = await tts_service.synthesize_stock(
        text="Hola",
        voice_id="es-mx-male",
        language="es",
        speed=1.2,
        emotion="happy",
    )
    assert isinstance(result, AudioResult)
    mock_client.synthesize_stock.assert_called_once()
    call_kwargs = mock_client.synthesize_stock.call_args.kwargs
    assert call_kwargs["speed"] == 1.2
    assert call_kwargs["emotion"] == "happy"


@pytest.mark.asyncio
async def test_synthesize_stock_voice_not_found(tts_service: TtsService) -> None:
    mock_client = AsyncMock(spec=OmniVoiceEngineClient)
    mock_client.list_stock_voices = AsyncMock(return_value=[])
    tts_service._engine_client = mock_client

    with pytest.raises(VoiceNotFoundError):
        await tts_service.synthesize_stock(
            text="Hola",
            voice_id="nonexistent",
            language="es",
        )


@pytest.mark.asyncio
async def test_synthesize_stock_unsupported_language(tts_service: TtsService) -> None:
    mock_client = AsyncMock(spec=OmniVoiceEngineClient)
    mock_client.list_stock_voices = AsyncMock(
        return_value=[
            MagicMock(voice_id="es-mx-male"),
        ]
    )
    tts_service._engine_client = mock_client
    tts_service._settings = MagicMock()
    tts_service._settings.omnilang_list = ["es", "en", "fr"]

    with pytest.raises(UnsupportedLanguageError):
        await tts_service.synthesize_stock(
            text="Hola",
            voice_id="es-mx-male",
            language="xx",
        )


@pytest.mark.asyncio
async def test_synthesize_instruct_success(tts_service: TtsService) -> None:
    mock_client = AsyncMock(spec=OmniVoiceEngineClient)
    mock_client.synthesize_instruct = AsyncMock(
        return_value=AudioResult(
            wav_bytes=b"RIFF" + b"\x00" * 100,
            duration_sec=1.0,
            sample_rate=22050,
        )
    )
    tts_service._engine_client = mock_client
    tts_service._settings = MagicMock()
    tts_service._settings.omnilang_list = ["es", "en"]

    result = await tts_service.synthesize_instruct(
        text="Hello",
        instruct="female, british accent",
        language="en",
    )
    assert isinstance(result, AudioResult)


@pytest.mark.asyncio
async def test_synthesize_clone_success(tts_service: TtsService) -> None:
    mock_voice_service = AsyncMock()
    mock_voice_service.get_voice = AsyncMock(
        return_value={
            "reference_path": "/tmp/ref.wav",
            "language": "es",
        }
    )
    tts_service._voice_service = mock_voice_service

    mock_client = AsyncMock(spec=OmniVoiceEngineClient)
    mock_client.synthesize_clone = AsyncMock(
        return_value=AudioResult(
            wav_bytes=b"RIFF" + b"\x00" * 100,
            duration_sec=1.0,
            sample_rate=22050,
        )
    )
    tts_service._engine_client = mock_client

    result = await tts_service.synthesize_clone(
        text="Hola",
        voice_id="cloned-123",
        language="es",
    )
    assert isinstance(result, AudioResult)
    mock_client.synthesize_clone.assert_called_once()


@pytest.mark.asyncio
async def test_synthesize_clone_uses_voice_language(tts_service: TtsService) -> None:
    mock_voice_service = AsyncMock()
    mock_voice_service.get_voice = AsyncMock(
        return_value={
            "reference_path": "/tmp/ref.wav",
            "language": "es",
        }
    )
    tts_service._voice_service = mock_voice_service

    mock_client = AsyncMock(spec=OmniVoiceEngineClient)
    mock_client.synthesize_clone = AsyncMock(
        return_value=AudioResult(
            wav_bytes=b"RIFF" + b"\x00" * 100,
            duration_sec=1.0,
            sample_rate=22050,
        )
    )
    tts_service._engine_client = mock_client

    # El API manda language="en", pero la voz es "es": debe usarse la de la DB.
    result = await tts_service.synthesize_clone(
        text="Hola",
        voice_id="cloned-123",
        language="en",
    )
    assert isinstance(result, AudioResult)
    call_kwargs = mock_client.synthesize_clone.call_args.kwargs
    assert call_kwargs["language"] == "es"
