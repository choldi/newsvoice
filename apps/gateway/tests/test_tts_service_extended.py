"""Tests adicionales para el servicio TTS (cubrir paths no testeados)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tts_api.core.engine_client import AudioResult
from tts_api.core.exceptions import (
    VoiceNotFoundError,
)
from tts_api.services.tts import TtsService


@pytest.mark.asyncio
async def test_synthesize_stock_fallback_to_stock_voice() -> None:
    """Test de fallback a voz stock cuando la voz clonada y diseñada no existen."""
    mock_engine = AsyncMock()
    mock_engine.list_stock_voices.return_value = [
        MagicMock(voice_id="es-mx-male", language="es"),
    ]
    mock_engine.synthesize_stock.return_value = AudioResult(
        wav_bytes=b"wav",
        duration_sec=1.0,
        sample_rate=22050,
    )

    mock_voice_service = AsyncMock()
    mock_voice_service.get_voice.side_effect = VoiceNotFoundError("es-mx-male")
    mock_voice_service.get_designed_voice.side_effect = VoiceNotFoundError("es-mx-male")

    service = TtsService(engine_client=mock_engine, voice_service=mock_voice_service)
    result = await service.synthesize_stock(
        text="Hola",
        voice_id="es-mx-male",
        language="es",
    )
    assert result.wav_bytes == b"wav"
    mock_engine.synthesize_stock.assert_called_once()


@pytest.mark.asyncio
async def test_synthesize_stock_fallback_on_other_exception() -> None:
    """Test de fallback cuando voice_service lanza otra excepción."""
    mock_engine = AsyncMock()
    mock_engine.list_stock_voices.return_value = [
        MagicMock(voice_id="es-mx-male", language="es"),
    ]
    mock_engine.synthesize_stock.return_value = AudioResult(
        wav_bytes=b"wav",
        duration_sec=1.0,
        sample_rate=22050,
    )

    mock_voice_service = AsyncMock()
    mock_voice_service.get_voice.side_effect = RuntimeError("DB error")
    mock_voice_service.get_designed_voice.side_effect = VoiceNotFoundError("es-mx-male")

    service = TtsService(engine_client=mock_engine, voice_service=mock_voice_service)
    result = await service.synthesize_stock(
        text="Hola",
        voice_id="es-mx-male",
        language="es",
    )
    assert result.wav_bytes == b"wav"


@pytest.mark.asyncio
async def test_synthesize_stock_cloned_voice() -> None:
    """Test de síntesis con voz clonada (voice found in repository)."""
    mock_engine = AsyncMock()
    mock_engine.synthesize_clone.return_value = AudioResult(
        wav_bytes=b"cloned-wav",
        duration_sec=1.0,
        sample_rate=22050,
    )

    mock_voice_service = AsyncMock()
    mock_voice_service.get_voice.return_value = {
        "id": "clone-id",
        "language": "es",
        "reference_path": "/path/to/ref.wav",
    }

    service = TtsService(engine_client=mock_engine, voice_service=mock_voice_service)
    result = await service.synthesize_stock(
        text="Hola",
        voice_id="clone-id",
        language="es",
    )
    assert result.wav_bytes == b"cloned-wav"
    mock_engine.synthesize_clone.assert_called_once()


@pytest.mark.asyncio
async def test_synthesize_stock_designed_voice() -> None:
    """Test de síntesis con voz diseñada (instruct preset from DB)."""
    mock_engine = AsyncMock()
    mock_engine.synthesize_instruct.return_value = AudioResult(
        wav_bytes=b"designed-wav",
        duration_sec=1.5,
        sample_rate=22050,
    )

    mock_voice_service = AsyncMock()
    mock_voice_service.get_voice.side_effect = VoiceNotFoundError("designed-id")
    mock_voice_service.get_designed_voice.return_value = {
        "id": "designed-id",
        "name": "British Female",
        "instruct": "female, young adult, british accent",
        "language": "en",
    }

    service = TtsService(engine_client=mock_engine, voice_service=mock_voice_service)
    result = await service.synthesize_stock(
        text="Hello",
        voice_id="designed-id",
        language="en",
    )
    assert result.wav_bytes == b"designed-wav"
    mock_engine.synthesize_instruct.assert_called_once_with(
        text="Hello",
        instruct="female, young adult, british accent",
        speed=1.0,
        emotion=None,
        language="en",
    )


@pytest.mark.asyncio
async def test_synthesize_stock_designed_voice_not_found_falls_to_stock() -> None:
    """Test de fallback a stock cuando la voz clonada y diseñada no existen."""
    mock_engine = AsyncMock()
    mock_engine.list_stock_voices.return_value = [
        MagicMock(voice_id="es-mx-male", language="es"),
    ]
    mock_engine.synthesize_stock.return_value = AudioResult(
        wav_bytes=b"stock-wav",
        duration_sec=1.0,
        sample_rate=22050,
    )

    mock_voice_service = AsyncMock()
    mock_voice_service.get_voice.side_effect = VoiceNotFoundError("es-mx-male")
    mock_voice_service.get_designed_voice.side_effect = VoiceNotFoundError("es-mx-male")

    service = TtsService(engine_client=mock_engine, voice_service=mock_voice_service)
    result = await service.synthesize_stock(
        text="Hola",
        voice_id="es-mx-male",
        language="es",
    )
    assert result.wav_bytes == b"stock-wav"
    mock_engine.synthesize_stock.assert_called_once()
    mock_voice_service.get_voice.assert_called_once_with("es-mx-male")
    mock_voice_service.get_designed_voice.assert_called_once_with("es-mx-male")


@pytest.mark.asyncio
async def test_synthesize_clone_uses_voice_language() -> None:
    """Test that synthesize_clone uses the voice's own language from DB."""
    mock_engine = AsyncMock()
    mock_engine.synthesize_clone.return_value = AudioResult(
        wav_bytes=b"cloned-wav",
        duration_sec=2.0,
        sample_rate=22050,
    )

    mock_voice_service = AsyncMock()
    mock_voice_service.get_voice.return_value = {
        "id": "clone-id",
        "language": "es",
        "reference_path": "/path/to/ref.wav",
    }

    service = TtsService(engine_client=mock_engine, voice_service=mock_voice_service)
    # API sends language="en" but voice is "es" — should use voice's language
    result = await service.synthesize_clone(
        text="Hola",
        voice_id="clone-id",
        language="en",
    )
    assert result.wav_bytes == b"cloned-wav"
    mock_engine.synthesize_clone.assert_called_once_with(
        text="Hola",
        reference_audio_path="/path/to/ref.wav",
        ref_text=None,
        speed=1.0,
        language="es",  # uses voice's language, not API parameter
    )


@pytest.mark.asyncio
async def test_synthesize_clone_success() -> None:
    """Test de síntesis con voz clonada exitosa."""
    mock_engine = AsyncMock()
    mock_engine.synthesize_clone.return_value = AudioResult(
        wav_bytes=b"cloned-wav",
        duration_sec=2.0,
        sample_rate=22050,
    )

    mock_voice_service = AsyncMock()
    mock_voice_service.get_voice.return_value = {
        "id": "clone-id",
        "language": "es",
        "reference_path": "/path/to/ref.wav",
    }

    service = TtsService(engine_client=mock_engine, voice_service=mock_voice_service)
    result = await service.synthesize_clone(
        text="Hola",
        voice_id="clone-id",
        language="es",
    )
    assert result.wav_bytes == b"cloned-wav"
    mock_engine.synthesize_clone.assert_called_once_with(
        text="Hola",
        reference_audio_path="/path/to/ref.wav",
        ref_text=None,
        speed=1.0,
        language="es",
    )


@pytest.mark.asyncio
async def test_get_engine_client_uses_shared() -> None:
    """Test de que _get_engine_client reutiliza el cliente compartido."""
    service = TtsService(engine_client=None, voice_service=AsyncMock())
    with patch("tts_api.services.tts.get_shared_engine_client") as shared_getter:
        mock_instance = AsyncMock()
        shared_getter.return_value = mock_instance
        client = await service._get_engine_client()
        assert client is mock_instance
        shared_getter.assert_called_once_with()
        mock_instance.start.assert_called_once()


@pytest.mark.asyncio
async def test_get_voice_service_creates_new() -> None:
    """Test de que _get_voice_service crea un servicio nuevo si es None."""
    service = TtsService(engine_client=AsyncMock(), voice_service=None)
    with patch("tts_api.services.tts.VoiceService") as voice_service_cls:
        mock_instance = AsyncMock()
        voice_service_cls.return_value = mock_instance
        vs = await service._get_voice_service()
        assert vs is mock_instance
        mock_instance.initialize.assert_called_once()


@pytest.mark.asyncio
async def test_close_no_engine() -> None:
    """Test de close cuando no hay engine client."""
    service = TtsService(engine_client=None)
    await service.close()  # Should not raise


# --- Conversation + cloned voice tests ---


@pytest.mark.asyncio
async def test_conversation_with_cloned_voice() -> None:
    """Test de conversación multi-voz con voz clonada."""
    from tts_api.services.conversation import ConversationService, ConversationTurn

    mock_tts_service = AsyncMock()
    mock_tts_service.synthesize_stock.return_value = AudioResult(
        wav_bytes=b"synthesized-audio",
        duration_sec=1.0,
        sample_rate=22050,
    )

    convo_service = ConversationService(tts_service=mock_tts_service)

    turns = [
        ConversationTurn(voice_id="es-mx-male", text="Hola", language="es"),
        ConversationTurn(voice_id="clone-uuid-1", text="Clon responde", language="es"),
    ]
    result = await convo_service.generate(turns=turns, pause_ms=200)

    assert result.wav_bytes is not None
    assert len(result.wav_bytes) > 0
    # Both turns should go through tts_service.synthesize_stock
    assert mock_tts_service.synthesize_stock.call_count == 2


@pytest.mark.asyncio
async def test_conversation_rejects_fewer_than_two_turns() -> None:
    """Test de error cuando hay menos de 2 turnos."""
    from tts_api.services.conversation import ConversationService, ConversationTurn

    tts_service = AsyncMock()
    convo_service = ConversationService(tts_service=tts_service)

    with pytest.raises(ValueError, match="al menos 2 turnos"):
        await convo_service.generate(
            turns=[ConversationTurn(voice_id="es-mx-male", text="Solo uno", language="es")],
        )


@pytest.mark.asyncio
async def test_conversation_rejects_empty_text() -> None:
    """Test de error cuando un turno tiene texto vacío."""
    from tts_api.services.conversation import ConversationService, ConversationTurn

    tts_service = AsyncMock()
    convo_service = ConversationService(tts_service=tts_service)

    turns = [
        ConversationTurn(voice_id="es-mx-male", text="", language="es"),
        ConversationTurn(voice_id="es-mx-female", text="Hola", language="es"),
    ]
    with pytest.raises(ValueError, match="texto vacío"):
        await convo_service.generate(turns=turns)


@pytest.mark.asyncio
async def test_close_no_descarga_el_engine_compartido() -> None:
    """close() suelta la referencia sin detener el cliente compartido."""
    client = AsyncMock()
    service = TtsService(engine_client=client, voice_service=AsyncMock())

    await service.close()

    client.stop.assert_not_awaited()
    assert service._engine_client is None
