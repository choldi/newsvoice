"""Tests unitarios para el servicio de voces."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from tts_api.core.audio import AudioValidator
from tts_api.core.exceptions import (
    InvalidReferenceAudioError,
    UnsupportedLanguageError,
    VoiceNotFoundError,
)
from tts_api.repositories.voice_repository import VoiceRepository
from tts_api.services.voice_service import VoiceService


@pytest.fixture
def mock_repository() -> AsyncMock:
    """Repositorio mockeado."""
    return AsyncMock(spec=VoiceRepository)


@pytest.fixture
def mock_audio_validator() -> AsyncMock:
    """Validador de audio mockeado."""
    return AsyncMock(spec=AudioValidator)


@pytest.fixture
def voice_service(mock_repository: AsyncMock, mock_audio_validator: AsyncMock) -> VoiceService:
    """Servicio de voces con dependencias mockeadas."""
    with patch("tts_api.services.voice_service.get_settings") as mock_settings:
        mock_settings.return_value.omnilang_list = ["es", "en", "zh"]
        mock_settings.return_value.TTS_ENGINE = "omnivoice"
        mock_settings.return_value.MAX_REFERENCE_DURATION_SEC = 30.0
        mock_settings.return_value.VOICES_DIR = Path("/tmp/test_voices")
        return VoiceService(
            repository=mock_repository,
            audio_validator=mock_audio_validator,
            embedding_cache=MagicMock(),
        )


@pytest.mark.asyncio
async def test_clone_voice_success(
    voice_service: VoiceService, mock_repository: AsyncMock, mock_audio_validator: AsyncMock
) -> None:
    """Test de clonación exitosa."""
    mock_audio_validator.validate_and_prepare.return_value = (
        Path("/tmp/processed.wav"),
        5.0,
    )
    mock_repository.create.return_value = str(uuid4())

    # clone_voice construye su propio AudioValidator por motor (sample rate),
    # así que hay que mockear ese nombre y no el inyectado en el servicio.
    with (
        patch("tts_api.services.voice_service.shutil"),
        patch(
            "tts_api.services.voice_service.AudioValidator",
            return_value=mock_audio_validator,
        ),
    ):
        voice_id = await voice_service.clone_voice(
            name="test-voice",
            language="es",
            reference_audio_path="/path/to/audio.wav",
        )
    assert voice_id is not None
    mock_repository.create.assert_called_once()


@pytest.mark.asyncio
async def test_clone_voice_unsupported_language(voice_service: VoiceService) -> None:
    """Test de error con idioma no soportado."""
    with pytest.raises(UnsupportedLanguageError):
        await voice_service.clone_voice(
            name="test-voice",
            language="xx",
            reference_audio_path="/path/to/audio.wav",
        )


@pytest.mark.asyncio
async def test_clone_voice_invalid_audio(
    voice_service: VoiceService, mock_audio_validator: AsyncMock
) -> None:
    """Test de error con audio inválido."""
    mock_audio_validator.validate_and_prepare.side_effect = InvalidReferenceAudioError("Bad audio")
    with pytest.raises(InvalidReferenceAudioError):
        await voice_service.clone_voice(
            name="test-voice",
            language="es",
            reference_audio_path="/path/to/bad.wav",
        )


@pytest.mark.asyncio
async def test_get_voice(voice_service: VoiceService, mock_repository: AsyncMock) -> None:
    """Test de obtener voz."""
    mock_repository.get_by_id.return_value = {"id": "test-id", "name": "test"}
    voice = await voice_service.get_voice("test-id")
    assert voice["id"] == "test-id"


@pytest.mark.asyncio
async def test_get_voice_not_found(voice_service: VoiceService, mock_repository: AsyncMock) -> None:
    """Test de obtener voz no encontrada."""
    mock_repository.get_by_id.side_effect = VoiceNotFoundError("test-id")
    with pytest.raises(VoiceNotFoundError):
        await voice_service.get_voice("test-id")


@pytest.mark.asyncio
async def test_list_voices(voice_service: VoiceService, mock_repository: AsyncMock) -> None:
    """Test de listar voces."""
    mock_repository.list.return_value = [{"id": "1"}, {"id": "2"}]
    voices = await voice_service.list_voices(language="es", limit=10, offset=0)
    assert len(voices) == 2
    mock_repository.list.assert_called_once_with(language="es", limit=10, offset=0, engine=None)


@pytest.mark.asyncio
async def test_delete_voice(voice_service: VoiceService, mock_repository: AsyncMock) -> None:
    """Test de eliminar voz."""
    mock_repository.get_by_id.return_value = {
        "id": "test-id",
        "reference_path": "/tmp/ref.wav",
    }
    mock_repository.delete.return_value = True
    result = await voice_service.delete_voice("test-id")
    assert result is True
    mock_repository.delete.assert_called_once_with("test-id")


@pytest.mark.asyncio
async def test_voice_exists(voice_service: VoiceService, mock_repository: AsyncMock) -> None:
    """Test de verificar existencia."""
    mock_repository.voice_exists.return_value = True
    assert await voice_service.voice_exists("test-id") is True


@pytest.mark.asyncio
async def test_initialize_creates_defaults() -> None:
    """Test de que initialize crea repository/validator/cache cuando no se inyectan."""
    with patch("tts_api.services.voice_service.get_settings") as mock_settings:
        mock_settings.return_value.omnilang_list = ["es"]
        mock_settings.return_value.DATABASE_URL = "sqlite+aiosqlite:///tmp/test.db"
        service = VoiceService()
        assert service._repository is None
        await service.initialize()
        assert service._repository is not None
        assert service._audio_validator is not None
        assert service._embedding_cache is not None


@pytest.mark.asyncio
async def test_initialize_idempotent() -> None:
    """Test de que initialize no recrea dependencias ya inyectadas."""
    with patch("tts_api.services.voice_service.get_settings") as mock_settings:
        mock_settings.return_value.omnilang_list = ["es"]
        mock_repo = AsyncMock(spec=VoiceRepository)
        mock_validator = AsyncMock(spec=AudioValidator)
        mock_cache = AsyncMock()
        service = VoiceService(
            repository=mock_repo, audio_validator=mock_validator, embedding_cache=mock_cache
        )
        await service.initialize()
        # Should not replace already-injected dependencies
        assert service._repository is mock_repo
        assert service._audio_validator is mock_validator
        assert service._embedding_cache is mock_cache
