"""Tests unitarios para el repositorio de voces."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from tts_api.core.exceptions import VoiceNotFoundError
from tts_api.repositories.voice_repository import VoiceRepository


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Ruta de base de datos temporal."""
    return str(tmp_path / "test_voices.db")


@pytest.fixture
def repo(db_path: str) -> VoiceRepository:
    """Repositorio con DB temporal."""
    with patch("tts_api.repositories.voice_repository.get_settings") as mock_settings:
        mock_settings.return_value.DATABASE_URL = f"sqlite:///{db_path}"
        return VoiceRepository(db_path=db_path)


@pytest.mark.asyncio
async def test_initialize_creates_schema(repo: VoiceRepository) -> None:
    """Test de inicialización crea el esquema."""
    await repo.initialize()
    # Verify table exists by trying to use it
    conn = await repo._get_connection()
    try:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cloned_voices'"
        )
        row = await cursor.fetchone()
        assert row is not None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_create_voice(repo: VoiceRepository) -> None:
    """Test de creación de voz."""
    await repo.initialize()
    voice_id = await repo.create(
        name="test-voice",
        language="es",
        reference_path="/path/to/audio.wav",
        duration_sec=5.0,
        metadata={"key": "value"},
    )
    assert voice_id is not None
    assert len(voice_id) == 36  # UUID format


@pytest.mark.asyncio
async def test_create_voice_duplicate_name(repo: VoiceRepository) -> None:
    """Test de error al crear voz con nombre duplicado."""
    await repo.initialize()
    await repo.create(
        name="test-voice",
        language="es",
        reference_path="/path/to/audio.wav",
        duration_sec=5.0,
    )
    with pytest.raises(ValueError, match="already exists"):
        await repo.create(
            name="test-voice",
            language="en",
            reference_path="/path/to/audio2.wav",
            duration_sec=3.0,
        )


@pytest.mark.asyncio
async def test_get_by_id_success(repo: VoiceRepository) -> None:
    """Test de obtener voz por ID."""
    await repo.initialize()
    voice_id = await repo.create(
        name="test-voice",
        language="es",
        reference_path="/path/to/audio.wav",
        duration_sec=5.0,
        metadata={"key": "value"},
    )
    voice = await repo.get_by_id(voice_id)
    assert voice["id"] == voice_id
    assert voice["name"] == "test-voice"
    assert voice["language"] == "es"
    assert voice["reference_path"] == "/path/to/audio.wav"
    assert voice["duration_sec"] == 5.0
    assert voice["metadata"] == {"key": "value"}


@pytest.mark.asyncio
async def test_get_by_id_not_found(repo: VoiceRepository) -> None:
    """Test de error al obtener voz inexistente por ID."""
    await repo.initialize()
    with pytest.raises(VoiceNotFoundError):
        await repo.get_by_id(str(uuid4()))


@pytest.mark.asyncio
async def test_get_by_name_success(repo: VoiceRepository) -> None:
    """Test de obtener voz por nombre."""
    await repo.initialize()
    voice_id = await repo.create(
        name="test-voice",
        language="es",
        reference_path="/path/to/audio.wav",
        duration_sec=5.0,
    )
    voice = await repo.get_by_name("test-voice")
    assert voice["id"] == voice_id
    assert voice["name"] == "test-voice"


@pytest.mark.asyncio
async def test_get_by_name_not_found(repo: VoiceRepository) -> None:
    """Test de error al obtener voz inexistente por nombre."""
    await repo.initialize()
    with pytest.raises(VoiceNotFoundError):
        await repo.get_by_name("nonexistent")


@pytest.mark.asyncio
async def test_list_all_voices(repo: VoiceRepository) -> None:
    """Test de listar todas las voces."""
    await repo.initialize()
    await repo.create(name="voice1", language="es", reference_path="/a.wav", duration_sec=1.0)
    await repo.create(name="voice2", language="en", reference_path="/b.wav", duration_sec=2.0)
    voices = await repo.list()
    assert len(voices) == 2


@pytest.mark.asyncio
async def test_list_filter_by_language(repo: VoiceRepository) -> None:
    """Test de listar voces filtradas por idioma."""
    await repo.initialize()
    await repo.create(name="voice1", language="es", reference_path="/a.wav", duration_sec=1.0)
    await repo.create(name="voice2", language="en", reference_path="/b.wav", duration_sec=2.0)
    voices = await repo.list(language="es")
    assert len(voices) == 1
    assert voices[0]["language"] == "es"


@pytest.mark.asyncio
async def test_list_with_pagination(repo: VoiceRepository) -> None:
    """Test de listar voces con paginación."""
    await repo.initialize()
    for i in range(5):
        await repo.create(
            name=f"voice{i}", language="es", reference_path=f"/{i}.wav", duration_sec=1.0
        )
    voices = await repo.list(limit=2, offset=0)
    assert len(voices) == 2
    voices = await repo.list(limit=2, offset=2)
    assert len(voices) == 2
    voices = await repo.list(limit=2, offset=4)
    assert len(voices) == 1


@pytest.mark.asyncio
async def test_update_name(repo: VoiceRepository) -> None:
    """Test de actualizar nombre de voz."""
    await repo.initialize()
    voice_id = await repo.create(
        name="old-name", language="es", reference_path="/a.wav", duration_sec=1.0
    )
    updated = await repo.update(voice_id, name="new-name")
    assert updated is True
    voice = await repo.get_by_id(voice_id)
    assert voice["name"] == "new-name"


@pytest.mark.asyncio
async def test_update_metadata(repo: VoiceRepository) -> None:
    """Test de actualizar metadata de voz."""
    await repo.initialize()
    voice_id = await repo.create(
        name="test-voice",
        language="es",
        reference_path="/a.wav",
        duration_sec=1.0,
        metadata={"key1": "val1"},
    )
    updated = await repo.update(voice_id, metadata={"key2": "val2"})
    assert updated is True
    voice = await repo.get_by_id(voice_id)
    assert voice["metadata"] == {"key1": "val1", "key2": "val2"}


@pytest.mark.asyncio
async def test_update_no_changes(repo: VoiceRepository) -> None:
    """Test de update sin cambios retorna False."""
    await repo.initialize()
    voice_id = await repo.create(
        name="test-voice", language="es", reference_path="/a.wav", duration_sec=1.0
    )
    updated = await repo.update(voice_id)
    assert updated is False


@pytest.mark.asyncio
async def test_update_not_found(repo: VoiceRepository) -> None:
    """Test de update en voz inexistente retorna False."""
    await repo.initialize()
    updated = await repo.update(str(uuid4()), name="new-name")
    assert updated is False


@pytest.mark.asyncio
async def test_delete_success(repo: VoiceRepository) -> None:
    """Test de eliminación de voz."""
    await repo.initialize()
    voice_id = await repo.create(
        name="test-voice", language="es", reference_path="/a.wav", duration_sec=1.0
    )
    deleted = await repo.delete(voice_id)
    assert deleted is True
    with pytest.raises(VoiceNotFoundError):
        await repo.get_by_id(voice_id)


@pytest.mark.asyncio
async def test_delete_not_found(repo: VoiceRepository) -> None:
    """Test de eliminación de voz inexistente retorna False."""
    await repo.initialize()
    deleted = await repo.delete(str(uuid4()))
    assert deleted is False


@pytest.mark.asyncio
async def test_voice_exists_true(repo: VoiceRepository) -> None:
    """Test de verificación de existencia - voz existe."""
    await repo.initialize()
    voice_id = await repo.create(
        name="test-voice", language="es", reference_path="/a.wav", duration_sec=1.0
    )
    assert await repo.voice_exists(voice_id) is True


@pytest.mark.asyncio
async def test_voice_exists_false(repo: VoiceRepository) -> None:
    """Test de verificación de existencia - voz no existe."""
    await repo.initialize()
    assert await repo.voice_exists(str(uuid4())) is False


@pytest.mark.asyncio
async def test_get_by_id_no_metadata(repo: VoiceRepository) -> None:
    """Test de obtener voz sin metadata."""
    await repo.initialize()
    voice_id = await repo.create(
        name="test-voice", language="es", reference_path="/a.wav", duration_sec=1.0
    )
    voice = await repo.get_by_id(voice_id)
    assert voice["metadata"] == {}


@pytest.mark.asyncio
async def test_list_empty(repo: VoiceRepository) -> None:
    """Test de listar voces vacío."""
    await repo.initialize()
    voices = await repo.list()
    assert voices == []


# --- Designed voices tests ---


@pytest.mark.asyncio
async def test_initialize_creates_designed_voices_schema(repo: VoiceRepository) -> None:
    """Test que initialize crea la tabla designed_voices."""
    await repo.initialize()
    conn = await repo._get_connection()
    try:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='designed_voices'"
        )
        row = await cursor.fetchone()
        assert row is not None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_create_designed_voice(repo: VoiceRepository) -> None:
    await repo.initialize()
    voice_id = await repo.create_designed_voice(
        name="british-female",
        instruct="female, young adult, british accent",
        language="en",
    )
    assert voice_id is not None
    assert len(voice_id) == 36


@pytest.mark.asyncio
async def test_create_designed_voice_duplicate_name(repo: VoiceRepository) -> None:
    await repo.initialize()
    await repo.create_designed_voice(
        name="british-female",
        instruct="female, young adult, british accent",
        language="en",
    )
    with pytest.raises(ValueError, match="already exists"):
        await repo.create_designed_voice(
            name="british-female",
            instruct="female, elderly, british accent",
            language="en",
        )


@pytest.mark.asyncio
async def test_get_designed_voice(repo: VoiceRepository) -> None:
    await repo.initialize()
    voice_id = await repo.create_designed_voice(
        name="british-female",
        instruct="female, young adult, british accent",
        language="en",
    )
    voice = await repo.get_designed_voice(voice_id)
    assert voice["id"] == voice_id
    assert voice["name"] == "british-female"
    assert voice["instruct"] == "female, young adult, british accent"
    assert voice["language"] == "en"


@pytest.mark.asyncio
async def test_get_designed_voice_not_found(repo: VoiceRepository) -> None:
    await repo.initialize()
    with pytest.raises(VoiceNotFoundError):
        await repo.get_designed_voice(str(uuid4()))


@pytest.mark.asyncio
async def test_get_designed_voice_by_name(repo: VoiceRepository) -> None:
    await repo.initialize()
    voice_id = await repo.create_designed_voice(
        name="british-female",
        instruct="female, young adult, british accent",
        language="en",
    )
    voice = await repo.get_designed_voice_by_name("british-female")
    assert voice["id"] == voice_id


@pytest.mark.asyncio
async def test_list_designed_voices(repo: VoiceRepository) -> None:
    await repo.initialize()
    await repo.create_designed_voice(name="v1", instruct="male", language="en")
    await repo.create_designed_voice(name="v2", instruct="female", language="es")
    voices = await repo.list_designed_voices()
    assert len(voices) == 2


@pytest.mark.asyncio
async def test_list_designed_voices_filter_language(repo: VoiceRepository) -> None:
    await repo.initialize()
    await repo.create_designed_voice(name="v1", instruct="male", language="en")
    await repo.create_designed_voice(name="v2", instruct="female", language="es")
    voices = await repo.list_designed_voices(language="en")
    assert len(voices) == 1
    assert voices[0]["language"] == "en"


@pytest.mark.asyncio
async def test_delete_designed_voice(repo: VoiceRepository) -> None:
    await repo.initialize()
    voice_id = await repo.create_designed_voice(
        name="british-female",
        instruct="female, young adult, british accent",
        language="en",
    )
    deleted = await repo.delete_designed_voice(voice_id)
    assert deleted is True
    with pytest.raises(VoiceNotFoundError):
        await repo.get_designed_voice(voice_id)


@pytest.mark.asyncio
async def test_delete_designed_voice_not_found(repo: VoiceRepository) -> None:
    await repo.initialize()
    deleted = await repo.delete_designed_voice(str(uuid4()))
    assert deleted is False


@pytest.mark.asyncio
async def test_update_designed_voice(repo: VoiceRepository) -> None:
    await repo.initialize()
    voice_id = await repo.create_designed_voice(
        name="british-female",
        instruct="female, young adult, british accent",
        language="en",
    )
    updated = await repo.update_designed_voice(voice_id, name="british-woman")
    assert updated is True
    voice = await repo.get_designed_voice(voice_id)
    assert voice["name"] == "british-woman"
