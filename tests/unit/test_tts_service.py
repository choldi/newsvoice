"""Unit tests for TTSService."""

from pathlib import Path

import pytest

from src.api.models.tts import TTSIn
from src.api.services.tts import (
    TTSService,
    _content_hash,
    _estimate_duration,
    _truncate_for_tts,
)


def test_content_hash_is_stable() -> None:
    """_content_hash is deterministic for the same input."""
    h1 = _content_hash("hello", "es-ES-ElviraNeural")
    h2 = _content_hash("hello", "es-ES-ElviraNeural")
    assert h1 == h2


def test_content_hash_changes_with_voice() -> None:
    """_content_hash differs when voice changes."""
    h1 = _content_hash("hello", "voice-a")
    h2 = _content_hash("hello", "voice-b")
    assert h1 != h2


def test_estimate_duration_scales_with_chars() -> None:
    """_estimate_duration grows with character count."""
    assert _estimate_duration(0) == 0.0
    assert _estimate_duration(150) > _estimate_duration(15)


def test_truncate_for_tts_short_text_unchanged() -> None:
    """_truncate_for_tts returns text unchanged when under the limit."""
    text = "short text"
    assert _truncate_for_tts(text, 100) == text


def test_truncate_for_tts_breaks_at_paragraph() -> None:
    """_truncate_for_tts prefers paragraph boundaries when truncating."""
    text = ("paragraph one. " * 50) + "\n\n" + ("paragraph two. " * 50)
    truncated = _truncate_for_tts(text, 500)
    assert len(truncated) <= 500
    assert "paragraph one" in truncated


@pytest.mark.asyncio
async def test_generate_tts_uses_cache(tmp_path: Path) -> None:
    """generate_tts returns cached=True when the audio file already exists."""
    service = TTSService(cache_dir=str(tmp_path))
    payload = TTSIn(markdown="hola mundo", voice="es-ES-ElviraNeural")

    expected_hash = _content_hash("hola mundo", "es-ES-ElviraNeural")
    cached_file = tmp_path / f"{expected_hash}.mp3"
    cached_file.write_bytes(b"fake-mp3")

    result = await service.generate_tts(payload)
    assert result.cached is True
    assert result.audio_path == str(cached_file)
    assert result.voice == "es-ES-ElviraNeural"


@pytest.mark.asyncio
async def test_generate_tts_calls_edge_tts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """generate_tts invokes edge-tts when no cache exists."""
    service = TTSService(cache_dir=str(tmp_path))

    called: dict[str, str] = {}

    class FakeCommunicate:
        def __init__(self, text: str, voice: str) -> None:
            called["text"] = text
            called["voice"] = voice

        async def save(self, path: str) -> None:
            called["path"] = path
            Path(path).write_bytes(b"fake-mp3-bytes")

    monkeypatch.setattr("src.api.services.tts.edge_tts.Communicate", FakeCommunicate)

    payload = TTSIn(markdown="contenido de prueba", voice="es-ES-ElviraNeural")
    result = await service.generate_tts(payload)

    assert result.cached is False
    assert called["text"] == "contenido de prueba"
    assert called["voice"] == "es-ES-ElviraNeural"
    assert result.char_count == len("contenido de prueba")
    assert result.audio_path.endswith(".mp3")
