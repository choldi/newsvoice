"""Tests de integración para el endpoint TTS."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from tts_api.api.v1.tts import get_tts_service
from tts_api.core.engine_client import AudioResult
from tts_api.core.exceptions import (
    UnsupportedInstructError,
    UnsupportedLanguageError,
    VoiceNotFoundError,
)
from tts_api.main import app


def _make_wav() -> bytes:
    return (
        b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
        b"\x01\x00\x01\x00\x44\xac\x00\x00\x88\x58\x01\x00"
        b"\x02\x00\x10\x00data\x00\x00\x00\x00"
    )


@pytest.mark.asyncio
async def test_tts_stock_success(async_client: AsyncClient) -> None:
    with patch("tts_api.api.v1.tts.get_tts_service") as mock_dep:
        mock_service = AsyncMock()
        mock_service.synthesize_stock.return_value = AudioResult(
            wav_bytes=_make_wav(),
            duration_sec=0.5,
            sample_rate=22050,
        )
        mock_dep.return_value.__aenter__ = AsyncMock(return_value=mock_service)
        mock_dep.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await async_client.post(
            "/api/v1/tts",
            json={"text": "Hola", "voice_id": "es-mx-male", "language": "es"},
        )
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_tts_instruct_success(async_client: AsyncClient) -> None:
    with patch("tts_api.api.v1.tts.get_tts_service") as mock_dep:
        mock_service = AsyncMock()
        mock_service.synthesize_instruct.return_value = AudioResult(
            wav_bytes=_make_wav(),
            duration_sec=0.5,
            sample_rate=22050,
        )
        mock_dep.return_value.__aenter__ = AsyncMock(return_value=mock_service)
        mock_dep.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await async_client.post(
            "/api/v1/tts",
            json={
                "text": "Hello",
                "voice_id": "es-mx-male",
                "language": "en",
                "instruct": "female, british accent",
            },
        )
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_tts_voice_not_found(async_client: AsyncClient) -> None:
    with patch("tts_api.api.v1.tts.get_tts_service") as mock_dep:
        mock_service = AsyncMock()
        err = VoiceNotFoundError("nonexistent", "stock")
        err.valid_voice_ids = ["es-mx-male", "en-us-male"]
        mock_service.synthesize_stock.side_effect = err
        mock_dep.return_value.__aenter__ = AsyncMock(return_value=mock_service)
        mock_dep.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await async_client.post(
            "/api/v1/tts",
            json={"text": "Hola", "voice_id": "nonexistent", "language": "es"},
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_tts_unsupported_language(async_client: AsyncClient) -> None:
    with patch("tts_api.api.v1.tts.TtsService") as mock_service_cls:
        mock_service = AsyncMock()
        mock_service.synthesize_stock = AsyncMock(
            side_effect=UnsupportedLanguageError("xx", ["es", "en"])
        )
        mock_service.close = AsyncMock()
        mock_service_cls.return_value = mock_service

        resp = await async_client.post(
            "/api/v1/tts",
            json={"text": "Hola", "voice_id": "es-mx-male", "language": "xx"},
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_tts_instruct_endpoint_invalid(async_client: AsyncClient) -> None:
    with patch("tts_api.api.v1.tts.get_tts_service") as mock_dep:
        mock_service = AsyncMock()
        mock_service.synthesize_instruct.side_effect = UnsupportedInstructError(
            "Mexican accent",
            {"mexican accent": None},
            ["male", "female"],
        )
        mock_dep.return_value.__aenter__ = AsyncMock(return_value=mock_service)
        mock_dep.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await async_client.post(
            "/api/v1/tts/instruct",
            json={"text": "Hello", "instruct": "Mexican accent", "language": "en"},
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_tts_voice_design_tokens(async_client: AsyncClient) -> None:
    resp = await async_client.get("/api/v1/tts/voice-design/tokens")
    assert resp.status_code == 200
    data = resp.json()
    assert "gender" in data
    assert "male" in data["gender"]
    assert "english_accent" in data
    assert "chinese_dialect" in data


# --- Emotion tests ---


@pytest.mark.asyncio
async def test_tts_stock_with_emotion(async_client: AsyncClient) -> None:
    with patch("tts_api.api.v1.tts.get_tts_service") as mock_dep:
        mock_service = AsyncMock()
        mock_service.synthesize_stock.return_value = AudioResult(
            wav_bytes=_make_wav(),
            duration_sec=0.5,
            sample_rate=22050,
        )
        mock_dep.return_value.__aenter__ = AsyncMock(return_value=mock_service)
        mock_dep.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await async_client.post(
            "/api/v1/tts",
            json={"text": "Hello!", "voice_id": "en-us-male", "language": "en", "emotion": "happy"},
        )
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_tts_unsupported_emotion(async_client: AsyncClient) -> None:
    resp = await async_client.post(
        "/api/v1/tts",
        json={"text": "Hello!", "voice_id": "en-us-male", "language": "en", "emotion": "bored"},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["detail"]["error_type"] == "unsupported_emotion"


@pytest.mark.asyncio
async def test_tts_instruct_with_emotion(async_client: AsyncClient) -> None:
    with patch("tts_api.api.v1.tts.get_tts_service") as mock_dep:
        mock_service = AsyncMock()
        mock_service.synthesize_instruct.return_value = AudioResult(
            wav_bytes=_make_wav(),
            duration_sec=0.5,
            sample_rate=22050,
        )
        mock_dep.return_value.__aenter__ = AsyncMock(return_value=mock_service)
        mock_dep.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await async_client.post(
            "/api/v1/tts/instruct",
            json={
                "text": "Hello!",
                "instruct": "female, british accent",
                "language": "en",
                "emotion": "singing",
            },
        )
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_tts_unsupported_emotion_instruct(async_client: AsyncClient) -> None:
    resp = await async_client.post(
        "/api/v1/tts/instruct",
        json={
            "text": "Hello!",
            "instruct": "female, british accent",
            "language": "en",
            "emotion": "bored",
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_emotions_endpoint(async_client: AsyncClient) -> None:
    resp = await async_client.get("/api/v1/emotions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 8
    ids = [e["id"] for e in data]
    assert "happy" in ids
    assert "singing" in ids
    assert "sad" in ids
    assert "angry" in ids


@pytest.mark.asyncio
async def test_tts_designed_voice_via_stock_endpoint(async_client: AsyncClient) -> None:
    """Test that a designed voice_id works through the /tts stock endpoint."""
    mock_service = AsyncMock()
    mock_service.synthesize_stock.return_value = AudioResult(
        wav_bytes=_make_wav(),
        duration_sec=0.5,
        sample_rate=22050,
    )

    app.dependency_overrides[get_tts_service] = lambda: mock_service
    try:
        resp = await async_client.post(
            "/api/v1/tts",
            json={"text": "Hello!", "voice_id": "designed-uuid-1", "language": "en"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_tts_cloned_voice_via_stock_endpoint(async_client: AsyncClient) -> None:
    """Test that a cloned voice_id works through the /tts stock endpoint."""
    mock_service = AsyncMock()
    mock_service.synthesize_stock.return_value = AudioResult(
        wav_bytes=_make_wav(),
        duration_sec=0.5,
        sample_rate=22050,
    )

    app.dependency_overrides[get_tts_service] = lambda: mock_service
    try:
        resp = await async_client.post(
            "/api/v1/tts",
            json={"text": "Hola voz clonada", "voice_id": "clone-uuid-abc", "language": "es"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"
        # Verify the service was called with the cloned voice_id
        mock_service.synthesize_stock.assert_called_once()
        call_kwargs = mock_service.synthesize_stock.call_args.kwargs
        assert call_kwargs["voice_id"] == "clone-uuid-abc"
        assert call_kwargs["language"] == "es"
    finally:
        app.dependency_overrides.clear()
