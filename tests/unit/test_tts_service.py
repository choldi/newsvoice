"""Unit tests for TTSService (Edge neural voices via edge-tts)."""

from pathlib import Path

import pytest

import src.api.services.tts as tts_module
from src.api.models.tts import TTSIn
from src.api.services.tts import (
    TTSService,
    _content_hash,
    _estimate_duration,
    _markdown_to_speech_text,
    _truncate_for_tts,
)

_FAKE_MP3 = b"\xff\xfb\x90\x64" * 64


class _FakeCommunicate:
    """Fake edge_tts.Communicate returning MP3-ish audio bytes."""

    def __init__(self, text: str, voice: str, **kwargs: object) -> None:
        self.text = text
        self.voice = voice

    async def stream(self):
        yield {"type": "audio", "data": _FAKE_MP3}


def _reset_voices_cache(monkeypatch: pytest.MonkeyPatch, voices: list[dict] | None = None) -> None:
    """Point the module voice cache at a fixed catalogue."""
    monkeypatch.setattr(tts_module, "_EDGE_VOICES", voices or [])


def test_content_hash_is_stable() -> None:
    """_content_hash is deterministic for the same input."""
    h1 = _content_hash("hello", "es-MX-JorgeNeural", "es")
    h2 = _content_hash("hello", "es-MX-JorgeNeural", "es")
    assert h1 == h2


def test_content_hash_changes_with_voice() -> None:
    """_content_hash differs when voice changes."""
    h1 = _content_hash("hello", "voice-a", "es")
    h2 = _content_hash("hello", "voice-b", "es")
    assert h1 != h2


def test_content_hash_changes_with_language() -> None:
    """_content_hash differs when language changes."""
    h1 = _content_hash("hello", "es-MX-JorgeNeural", "es")
    h2 = _content_hash("hello", "es-MX-JorgeNeural", "en")
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


def test_markdown_to_speech_text_strips_markdown_keeps_emotion() -> None:
    """_markdown_to_speech_text removes markdown but keeps emotion tags."""
    markdown = "# Title\n\n**Bold** and *italic* [link](https://x.com)\n- Bullet [happy] text"
    speech = _markdown_to_speech_text(markdown)
    assert "Title" in speech
    assert "Bold" in speech
    assert "#" not in speech.split("\n")[0]
    assert "[happy]" in speech
    assert "https://x.com" not in speech


async def test_generate_tts_uses_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """generate_tts returns cached=True when the audio file already exists."""
    _reset_voices_cache(monkeypatch)
    monkeypatch.setattr(tts_module.edge_tts, "Communicate", _FakeCommunicate)
    service = TTSService(cache_dir=str(tmp_path))
    payload = TTSIn(markdown="hola mundo", voice="es-MX-JorgeNeural", language="es")

    expected_hash = _content_hash("hola mundo", "es-MX-JorgeNeural", "es")
    cached_file = tmp_path / f"{expected_hash}.mp3"
    cached_file.write_bytes(_FAKE_MP3)

    result = await service.generate_tts(payload)
    assert result.cached is True
    assert result.audio_path == str(cached_file)
    assert result.voice == "es-MX-JorgeNeural"


async def test_generate_tts_synthesizes_with_edge_voice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """generate_tts writes an MP3 synthesized by the resolved Edge voice."""
    _reset_voices_cache(monkeypatch)
    synthesis: list[tuple[str, str]] = []

    class RecordingCommunicate(_FakeCommunicate):
        async def stream(self):
            synthesis.append((self.text, self.voice))
            yield {"type": "audio", "data": _FAKE_MP3}

    monkeypatch.setattr(tts_module.edge_tts, "Communicate", RecordingCommunicate)
    service = TTSService(cache_dir=str(tmp_path))

    payload = TTSIn(markdown="contenido de prueba", voice="es-mx-male", language="es")
    result = await service.generate_tts(payload)

    assert result.cached is False
    assert result.audio_path.endswith(".mp3")
    assert Path(result.audio_path).exists()
    assert len(synthesis) == 1
    synthesized_text, used_voice = synthesis[0]
    assert synthesized_text == "contenido de prueba"
    assert used_voice == "es-MX-JorgeNeural"


async def test_resolve_voice_legacy_alias_maps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy OmniVoice names are mapped to Edge neural voices."""
    _reset_voices_cache(monkeypatch)
    service = TTSService()
    assert await service._resolve_voice("es-mx-male", "es") == "es-MX-JorgeNeural"
    assert await service._resolve_voice("en-gb-female", "en") == "en-GB-SoniaNeural"


async def test_resolve_voice_edge_shortname_matched(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exact Edge ShortName passes through unchanged."""
    _reset_voices_cache(monkeypatch, [{"ShortName": "es-ES-ElviraNeural"}])
    service = TTSService()
    assert await service._resolve_voice("es-ES-ElviraNeural", "es") == "es-ES-ElviraNeural"


async def test_resolve_voice_unknown_falls_back_to_language_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown voice names fall back to the language default, no network needed."""
    _reset_voices_cache(monkeypatch, [{"ShortName": "en-US-AriaNeural"}])
    service = TTSService()
    assert await service._resolve_voice("nueva-voz", "es") == "es-MX-JorgeNeural"


async def test_resolve_voice_empty_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty voice falls back to the language default."""
    _reset_voices_cache(monkeypatch)
    service = TTSService()
    assert await service._resolve_voice("", "en") == "en-US-GuyNeural"


async def test_synthesize_streams_mp3_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """_synthesize collects the audio chunks into MP3 bytes."""
    monkeypatch.setattr(tts_module.edge_tts, "Communicate", _FakeCommunicate)
    service = TTSService()
    data = await service._synthesize("hola", "es-MX-JorgeNeural")
    assert data == _FAKE_MP3


async def test_synthesize_empty_audio_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """_synthesize raises when no audio chunks arrive."""

    class EmptyCommunicate:
        def __init__(self, text: str, voice: str, **kwargs: object) -> None:
            pass

        async def stream(self):
            if False:
                yield {"type": "audio", "data": _FAKE_MP3}

    monkeypatch.setattr(tts_module.edge_tts, "Communicate", EmptyCommunicate)
    service = TTSService()
    with pytest.raises(RuntimeError, match="produced no audio"):
        await service._synthesize("hola", "es-MX-JorgeNeural")


async def test_synthesize_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """_synthesize raises a RuntimeError when the stream exceeds the timeout."""

    class SlowCommunicate:
        def __init__(self, text: str, voice: str, **kwargs: object) -> None:
            pass

        async def stream(self):
            import asyncio

            await asyncio.sleep(0.2)
            yield {"type": "audio", "data": _FAKE_MP3}

    monkeypatch.setattr(tts_module.edge_tts, "Communicate", SlowCommunicate)
    service = TTSService()
    service._request_timeout = 0.05
    with pytest.raises(RuntimeError, match="timed out"):
        await service._synthesize("hola", "es-MX-JorgeNeural")


async def test_generate_tts_empty_markdown_after_cleanup_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A markdown that reduces to nothing raises instead of calling Edge."""
    _reset_voices_cache(monkeypatch)
    service = TTSService(cache_dir=str(tmp_path))
    with pytest.raises(RuntimeError, match="No text to synthesize"):
        await service.generate_tts(TTSIn(markdown="####   \n"))
