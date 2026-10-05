"""Unit tests for TTSService (Edge neural voices via edge-tts)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import src.api.services.tts as tts_module
from src.api.models.tts import TTSIn
from src.api.services.tts import (
    TTSService,
    _audio_filename,
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


def test_audio_filename_pattern() -> None:
    """_audio_filename follows news_<collection>_<yymmdd>.mp3."""
    day = datetime(2026, 9, 21, tzinfo=UTC)
    assert _audio_filename("health", day) == "news_health_260921.mp3"


def test_audio_filename_sanitizes_collection() -> None:
    """Unsafe collection characters are replaced; empty falls back to default."""
    day = datetime(2026, 9, 21, tzinfo=UTC)
    assert _audio_filename(" Health Final ", day) == "news_health_final_260921.mp3"
    assert _audio_filename("!!", day) == "news_default_260921.mp3"


def test_audio_filename_defaults_to_now() -> None:
    """_audio_filename uses the current UTC day when no date is given."""
    filename = _audio_filename("tech", None)
    assert filename.startswith("news_tech_")
    assert filename.endswith(".mp3")
    assert len(filename) == len("news_tech_") + 6 + len(".mp3")


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
    """generate_tts returns cached=True when audio and sidecar match the content."""
    _reset_voices_cache(monkeypatch)
    synthesis: list[str] = []

    class RecordingCommunicate(_FakeCommunicate):
        async def stream(self):
            synthesis.append(self.text)
            yield {"type": "audio", "data": _FAKE_MP3}

    monkeypatch.setattr(tts_module.edge_tts, "Communicate", RecordingCommunicate)
    service = TTSService(cache_dir=str(tmp_path))
    payload = TTSIn(
        markdown="hola mundo",
        voice="es-MX-JorgeNeural",
        language="es",
        collection="health",
    )

    first = await service.generate_tts(payload)
    assert first.cached is False
    assert len(synthesis) == 1

    second = await service.generate_tts(payload)
    assert second.cached is True
    assert second.audio_path == first.audio_path
    assert len(synthesis) == 1
    assert Path(second.audio_path).read_bytes() == _FAKE_MP3


async def test_generate_tts_regenerates_when_markdown_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editing the digest invalidates the cached audio (the P1 bug)."""
    _reset_voices_cache(monkeypatch)
    synthesis: list[str] = []

    class RecordingCommunicate(_FakeCommunicate):
        async def stream(self):
            synthesis.append(self.text)
            yield {"type": "audio", "data": _FAKE_MP3 + b"new" * 10}

    monkeypatch.setattr(tts_module.edge_tts, "Communicate", RecordingCommunicate)
    service = TTSService(cache_dir=str(tmp_path))
    base = {"voice": "es-MX-JorgeNeural", "language": "es", "collection": "health"}

    first = await service.generate_tts(TTSIn(markdown="**hola** mundo", **base))
    assert first.cached is False

    same = await service.generate_tts(TTSIn(markdown="hola mundo", **base))
    assert same.cached is True
    assert len(synthesis) == 1

    changed = await service.generate_tts(TTSIn(markdown="hola mundo Again", **base))
    assert changed.cached is False
    assert changed.audio_path == first.audio_path
    assert len(synthesis) == 2
    assert Path(changed.audio_path).read_bytes() == _FAKE_MP3 + b"new" * 10


async def test_generate_tts_regenerates_when_voice_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different resolved voice for the same text invalidates the cache."""
    _reset_voices_cache(monkeypatch)
    synthesis: list[tuple[str, str]] = []

    class RecordingCommunicate(_FakeCommunicate):
        async def stream(self):
            synthesis.append((self.text, self.voice))
            yield {"type": "audio", "data": _FAKE_MP3}

    monkeypatch.setattr(tts_module.edge_tts, "Communicate", RecordingCommunicate)
    service = TTSService(cache_dir=str(tmp_path))
    first = await service.generate_tts(
        TTSIn(markdown="hola", voice="es-MX-JorgeNeural", language="es", collection="tech")
    )
    assert first.cached is False

    second = await service.generate_tts(
        TTSIn(markdown="hola", voice="es-mx-female", language="es", collection="tech")
    )
    assert second.cached is False
    assert synthesis[-1][1] == "es-MX-DaliaNeural"


async def test_generate_tts_regenerates_when_sidecar_is_missing_or_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy or corrupted cache without a matching sidecar is regenerated."""
    _reset_voices_cache(monkeypatch)
    synthesis: list[str] = []

    class RecordingCommunicate(_FakeCommunicate):
        async def stream(self):
            synthesis.append(self.text)
            yield {"type": "audio", "data": _FAKE_MP3}

    monkeypatch.setattr(tts_module.edge_tts, "Communicate", RecordingCommunicate)
    service = TTSService(cache_dir=str(tmp_path))
    payload = TTSIn(markdown="hola", voice="es-MX-JorgeNeural", language="es", collection="tech")

    audio = tmp_path / _audio_filename("tech")
    audio.write_bytes(_FAKE_MP3)
    legacy = await service.generate_tts(payload)
    assert legacy.cached is False
    assert len(synthesis) == 1

    sidecar = audio.with_suffix(".meta.json")
    sidecar.write_text("{not json", encoding="utf-8")
    corrupt = await service.generate_tts(payload)
    assert corrupt.cached is False
    assert len(synthesis) == 2

    stale = await service.generate_tts(payload)
    assert stale.cached is True
    assert len(synthesis) == 2


async def test_generate_tts_sidecar_records_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sidecar stores the resolved voice, language, chars and content hash."""
    _reset_voices_cache(monkeypatch)
    monkeypatch.setattr(tts_module.edge_tts, "Communicate", _FakeCommunicate)
    service = TTSService(cache_dir=str(tmp_path))

    result = await service.generate_tts(
        TTSIn(markdown="hola mundo", voice="es-mx-male", language="es", collection="health")
    )

    meta = json.loads(Path(result.audio_path).with_suffix(".meta.json").read_text("utf-8"))
    assert meta["version"] == tts_module.SIDECAR_VERSION
    assert meta["voice"] == "es-MX-JorgeNeural"
    assert meta["language"] == "es"
    assert meta["char_count"] == len("hola mundo")
    assert meta["sha256"] == tts_module._content_hash("hola mundo", "es-MX-JorgeNeural", "es")
    assert meta["generated_at"]


async def test_generate_tts_leaves_no_temp_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomic writes clean up their temporary files."""
    _reset_voices_cache(monkeypatch)
    monkeypatch.setattr(tts_module.edge_tts, "Communicate", _FakeCommunicate)
    service = TTSService(cache_dir=str(tmp_path))

    await service.generate_tts(
        TTSIn(markdown="hola", voice="es-MX-JorgeNeural", language="es", collection="tech")
    )

    audio = tmp_path / _audio_filename("tech")
    assert audio.exists()
    assert audio.with_suffix(".meta.json").exists()
    assert list(tmp_path.glob("*.tmp")) == []


async def test_generate_tts_synthesizes_with_edge_voice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """generate_tts writes an MP3 named news_<collection>_<yymmdd>.mp3."""
    _reset_voices_cache(monkeypatch)
    synthesis: list[tuple[str, str]] = []

    class RecordingCommunicate(_FakeCommunicate):
        async def stream(self):
            synthesis.append((self.text, self.voice))
            yield {"type": "audio", "data": _FAKE_MP3}

    monkeypatch.setattr(tts_module.edge_tts, "Communicate", RecordingCommunicate)
    service = TTSService(cache_dir=str(tmp_path))

    payload = TTSIn(
        markdown="contenido de prueba",
        voice="es-mx-male",
        language="es",
        collection="health",
    )
    result = await service.generate_tts(payload)

    assert result.cached is False
    assert result.audio_path.endswith(".mp3")
    assert Path(result.audio_path).name.startswith("news_health_")
    assert len(Path(result.audio_path).name) == len("news_health_") + 6 + len(".mp3")
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
    """synthesize_mp3 collects the audio chunks into MP3 bytes."""
    monkeypatch.setattr(tts_module.edge_tts, "Communicate", _FakeCommunicate)
    data = await tts_module.synthesize_mp3("hola", "es-MX-JorgeNeural")
    assert data == _FAKE_MP3


async def test_synthesize_empty_audio_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """synthesize_mp3 raises when no audio chunks arrive."""

    class EmptyCommunicate:
        def __init__(self, text: str, voice: str, **kwargs: object) -> None:
            pass

        async def stream(self):
            if False:
                yield {"type": "audio", "data": _FAKE_MP3}

    monkeypatch.setattr(tts_module.edge_tts, "Communicate", EmptyCommunicate)
    with pytest.raises(RuntimeError, match="produced no audio"):
        await tts_module.synthesize_mp3("hola", "es-MX-JorgeNeural")


async def test_synthesize_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """synthesize_mp3 raises a RuntimeError when the stream exceeds the timeout."""

    class SlowCommunicate:
        def __init__(self, text: str, voice: str, **kwargs: object) -> None:
            pass

        async def stream(self):
            import asyncio

            await asyncio.sleep(0.2)
            yield {"type": "audio", "data": _FAKE_MP3}

    monkeypatch.setattr(tts_module.edge_tts, "Communicate", SlowCommunicate)
    with pytest.raises(RuntimeError, match="timed out"):
        await tts_module.synthesize_mp3("hola", "es-MX-JorgeNeural", timeout=0)


async def test_generate_tts_empty_markdown_after_cleanup_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A markdown that reduces to nothing raises instead of calling Edge."""
    _reset_voices_cache(monkeypatch)
    service = TTSService(cache_dir=str(tmp_path))
    with pytest.raises(RuntimeError, match="No text to synthesize"):
        await service.generate_tts(TTSIn(markdown="####   \n"))


# --- Phase 8.5: wall-clock forecast ------------------------------------------


async def test_generate_tts_reports_the_wall_clock_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cold synthesis says how long it is expected to take."""
    _reset_voices_cache(monkeypatch)
    monkeypatch.setattr(tts_module.edge_tts, "Communicate", _FakeCommunicate)
    monkeypatch.setattr(tts_module.get_settings(), "tts_edge_chars_per_second", 40.0)
    monkeypatch.setattr(tts_module.get_settings(), "tts_chunk_overhead_seconds", 0.0)
    monkeypatch.setattr(tts_module.get_settings(), "tts_join_overhead_seconds", 0.0)
    service = TTSService(cache_dir=str(tmp_path))

    result = await service.generate_tts(
        TTSIn(markdown="hola mundo", voice="es-MX-JorgeNeural", language="es", collection="t")
    )

    assert result.estimate_wall_seconds is not None
    assert result.estimate_wall_seconds > 0
    assert result.estimate_wall_seconds == pytest.approx(len("hola mundo") / 40.0, abs=0.01)


async def test_generate_tts_reports_zero_for_a_cache_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache hit costs no waiting, and must say so instead of re-promising."""
    _reset_voices_cache(monkeypatch)
    monkeypatch.setattr(tts_module.edge_tts, "Communicate", _FakeCommunicate)
    service = TTSService(cache_dir=str(tmp_path))
    payload = TTSIn(
        markdown="contenido repetible", voice="es-MX-JorgeNeural", language="es", collection="t"
    )

    first = await service.generate_tts(payload)
    second = await service.generate_tts(payload)

    assert first.estimate_wall_seconds > 0
    assert second.cached is True
    assert second.estimate_wall_seconds == 0.0
