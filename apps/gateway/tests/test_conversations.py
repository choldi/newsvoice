"""Tests para conversaciones multi-voz."""

from __future__ import annotations

import io
import struct
import wave
from unittest.mock import AsyncMock, patch

import pytest

from tts_api.core.engine_client import AudioResult
from tts_api.core.exceptions import VoiceNotFoundError
from tts_api.services.conversation import ConversationService, ConversationTurn


def _make_wav() -> bytes:
    return (
        b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
        b"\x01\x00\x01\x00\x44\xac\x00\x00\x88\x58\x01\x00"
        b"\x02\x00\x10\x00data\x00\x00\x00\x00"
    )


def _make_real_wav(duration_sec: float = 0.1, sample_rate: int = 22050) -> bytes:
    n = int(duration_sec * sample_rate)
    frames = struct.pack(f"<{n}h", *([1000] * n))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(frames)
    return buf.getvalue()


# --- Unit tests for ConversationService ---


@pytest.mark.asyncio
async def test_conversation_minimum_turns():
    service = ConversationService(tts_service=AsyncMock())
    with pytest.raises(ValueError, match="al menos 2 turnos"):
        await service.generate(turns=[ConversationTurn(voice_id="a", text="hi")])


@pytest.mark.asyncio
async def test_conversation_empty_text():
    service = ConversationService(tts_service=AsyncMock())
    with pytest.raises(ValueError, match="texto vacío"):
        await service.generate(
            turns=[
                ConversationTurn(voice_id="es-mx-male", text=""),
                ConversationTurn(voice_id="es-mx-male", text="hello"),
            ]
        )


@pytest.mark.asyncio
async def test_conversation_voice_not_found():
    mock_tts = AsyncMock()
    mock_tts.synthesize_stock.side_effect = VoiceNotFoundError("nonexistent", "stock")
    service = ConversationService(tts_service=mock_tts)
    with pytest.raises(VoiceNotFoundError):
        await service.generate(
            turns=[
                ConversationTurn(voice_id="nonexistent", text="hello", language="es"),
                ConversationTurn(voice_id="nonexistent", text="world", language="es"),
            ]
        )


@pytest.mark.asyncio
async def test_conversation_success():
    mock_tts = AsyncMock()
    mock_tts.synthesize_stock.return_value = AudioResult(
        wav_bytes=_make_wav(),
        duration_sec=0.5,
        sample_rate=22050,
    )
    service = ConversationService(tts_service=mock_tts)

    result = await service.generate(
        turns=[
            ConversationTurn(voice_id="es-mx-male", text="Hola", language="es"),
            ConversationTurn(voice_id="es-mx-female", text="¿Qué tal?", language="es"),
        ]
    )

    assert isinstance(result, AudioResult)
    assert result.wav_bytes[:4] == b"RIFF"
    assert mock_tts.synthesize_stock.call_count == 2
    assert all(call.kwargs["speed"] == 1.0 for call in mock_tts.synthesize_stock.call_args_list)


@pytest.mark.asyncio
async def test_conversation_forwards_speed_to_every_turn():
    """El factor de velocidad llega a cada turno sin tocar los pause_ms."""
    mock_tts = AsyncMock()
    mock_tts.synthesize_stock.return_value = AudioResult(
        wav_bytes=_make_wav(),
        duration_sec=0.5,
        sample_rate=22050,
    )
    service = ConversationService(tts_service=mock_tts)

    result = await service.generate(
        turns=[
            ConversationTurn(voice_id="es-mx-male", text="Hola", language="es"),
            ConversationTurn(voice_id="es-mx-female", text="¿Qué tal?", language="es"),
        ],
        pause_ms=300,
        speed=1.5,
    )

    assert isinstance(result, AudioResult)
    assert mock_tts.synthesize_stock.call_count == 2
    assert all(call.kwargs["speed"] == 1.5 for call in mock_tts.synthesize_stock.call_args_list)


@pytest.mark.asyncio
async def test_conversation_multiple_turns():
    mock_tts = AsyncMock()
    mock_tts.synthesize_stock.return_value = AudioResult(
        wav_bytes=_make_wav(),
        duration_sec=0.5,
        sample_rate=22050,
    )
    service = ConversationService(tts_service=mock_tts)

    result = await service.generate(
        turns=[
            ConversationTurn(voice_id="v1", text="First", language="es"),
            ConversationTurn(voice_id="v2", text="Second", language="es"),
            ConversationTurn(voice_id="v3", text="Third", language="en"),
        ]
    )

    assert isinstance(result, AudioResult)
    assert mock_tts.synthesize_stock.call_count == 3


@pytest.mark.asyncio
async def test_conversation_concatenation():
    """Verify that the concatenated WAV is larger than a single segment."""
    mock_tts = AsyncMock()
    wav1 = _make_real_wav(0.1)
    wav2 = _make_real_wav(0.1)

    mock_tts.synthesize_stock = AsyncMock(
        side_effect=[
            AudioResult(wav_bytes=wav1, duration_sec=0.1, sample_rate=22050),
            AudioResult(wav_bytes=wav2, duration_sec=0.1, sample_rate=22050),
        ]
    )
    service = ConversationService(tts_service=mock_tts)

    result = await service.generate(
        turns=[
            ConversationTurn(voice_id="v1", text="First", language="es"),
            ConversationTurn(voice_id="v2", text="Second", language="es"),
        ],
        pause_ms=200,
    )

    assert result.wav_bytes[:4] == b"RIFF"
    assert len(result.wav_bytes) > len(wav1) + len(wav2)


# --- API tests ---


@pytest.mark.asyncio
async def test_conversations_endpoint_success(async_client):
    with patch("tts_api.api.v1.conversations.get_conversation_service") as mock_dep:
        mock_service = AsyncMock()
        mock_service.generate.return_value = AudioResult(
            wav_bytes=_make_wav(),
            duration_sec=1.0,
            sample_rate=22050,
        )
        mock_dep.return_value.__aenter__ = AsyncMock(return_value=mock_service)
        mock_dep.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await async_client.post(
            "/api/v1/conversations",
            json={
                "turns": [
                    {"voice_id": "es-mx-male", "text": "Hola", "language": "es"},
                    {"voice_id": "es-mx-female", "text": "¿Qué tal?", "language": "es"},
                ],
                "pause_ms": 300,
            },
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"


@pytest.mark.asyncio
async def test_conversations_endpoint_too_few_turns(async_client):
    resp = await async_client.post(
        "/api/v1/conversations",
        json={
            "turns": [{"voice_id": "es-mx-male", "text": "Solo uno"}],
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_conversations_endpoint_empty_text(async_client):
    resp = await async_client.post(
        "/api/v1/conversations",
        json={
            "turns": [
                {"voice_id": "es-mx-male", "text": ""},
                {"voice_id": "es-mx-female", "text": "Hello"},
            ],
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_conversations_endpoint_missing_fields(async_client):
    resp = await async_client.post(
        "/api/v1/conversations",
        json={
            "turns": [
                {"voice_id": "es-mx-male"},  # missing text
                {"text": "Hello"},  # missing voice_id
            ],
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_conversations_endpoint_voice_not_found(async_client):
    with patch("tts_api.api.v1.conversations.get_conversation_service") as mock_dep:
        mock_service = AsyncMock()
        mock_service.generate.side_effect = VoiceNotFoundError("nonexistent", "stock")
        mock_dep.return_value.__aenter__ = AsyncMock(return_value=mock_service)
        mock_dep.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await async_client.post(
            "/api/v1/conversations",
            json={
                "turns": [
                    {"voice_id": "nonexistent", "text": "Hello", "language": "es"},
                    {"voice_id": "es-mx-female", "text": "World", "language": "es"},
                ],
            },
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_conversations_endpoint_custom_pause(async_client):
    from tts_api.api.v1.conversations import get_conversation_service
    from tts_api.main import app

    mock_service = AsyncMock()
    mock_service.generate.return_value = AudioResult(
        wav_bytes=_make_wav(),
        duration_sec=1.0,
        sample_rate=22050,
    )
    app.dependency_overrides[get_conversation_service] = lambda: mock_service
    try:
        resp = await async_client.post(
            "/api/v1/conversations",
            json={
                "turns": [
                    {"voice_id": "es-mx-male", "text": "First", "language": "es"},
                    {"voice_id": "es-mx-female", "text": "Second", "language": "es"},
                ],
                "pause_ms": 1000,
            },
        )
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_conversations_endpoint_forwards_speed(async_client):
    from tts_api.api.v1.conversations import get_conversation_service
    from tts_api.main import app

    mock_service = AsyncMock()
    mock_service.generate.return_value = AudioResult(
        wav_bytes=_make_wav(),
        duration_sec=1.0,
        sample_rate=22050,
    )
    app.dependency_overrides[get_conversation_service] = lambda: mock_service
    try:
        resp = await async_client.post(
            "/api/v1/conversations",
            json={
                "turns": [
                    {"voice_id": "es-mx-male", "text": "First", "language": "es"},
                    {"voice_id": "es-mx-female", "text": "Second", "language": "es"},
                ],
                "pause_ms": 300,
                "speed": 1.2,
            },
        )
        assert resp.status_code == 200
        assert mock_service.generate.call_args.kwargs["speed"] == 1.2
        assert mock_service.generate.call_args.kwargs["pause_ms"] == 300
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_conversations_endpoint_speed_out_of_range(async_client):
    """speed fuera de 0.5-2.0 se rechaza en validación (422)."""
    resp = await async_client.post(
        "/api/v1/conversations",
        json={
            "turns": [
                {"voice_id": "es-mx-male", "text": "First", "language": "es"},
                {"voice_id": "es-mx-female", "text": "Second", "language": "es"},
            ],
            "speed": 3.0,
        },
    )
    assert resp.status_code == 422
