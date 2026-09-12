"""Unit tests for TTSService (OmniVoice API backend)."""

import io
import wave
from pathlib import Path

import pytest

from src.api.models.tts import TTSIn
from src.api.services.tts import (
    TTSService,
    _concat_wav,
    _content_hash,
    _estimate_duration,
    _fragment_text,
    _markdown_to_speech_text,
    _truncate_for_tts,
)


def _fake_wav(rate: int = 22050) -> bytes:
    """Return a valid 1-second silent WAV payload for tests."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * rate)
    return buffer.getvalue()


def test_content_hash_is_stable() -> None:
    """_content_hash is deterministic for the same input."""
    h1 = _content_hash("hello", "es-mx-male", "es")
    h2 = _content_hash("hello", "es-mx-male", "es")
    assert h1 == h2


def test_content_hash_changes_with_voice() -> None:
    """_content_hash differs when voice changes."""
    h1 = _content_hash("hello", "voice-a", "es")
    h2 = _content_hash("hello", "voice-b", "es")
    assert h1 != h2


def test_content_hash_changes_with_language() -> None:
    """_content_hash differs when language changes."""
    h1 = _content_hash("hello", "es-mx-male", "es")
    h2 = _content_hash("hello", "es-mx-male", "en")
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


def test_fragment_text_keeps_short_paragraphs() -> None:
    """_fragment_text keeps paragraphs under the limit intact."""
    text = "Primera historia.\nSegunda historia."
    fragments = _fragment_text(text, max_chars=400)
    assert fragments == ["Primera historia.", "Segunda historia."]


def test_fragment_text_splits_long_paragraphs_by_sentences() -> None:
    """_fragment_text splits long paragraphs at sentence boundaries."""
    text = "Frase muy larga sobre un tema importante. " * 20
    fragments = _fragment_text(text, max_chars=100)
    assert len(fragments) > 1
    assert all(len(f) <= 100 for f in fragments)


def test_fragment_text_keeps_emotion_tag_with_fragment() -> None:
    """_fragment_text keeps [emotion] tags at the start of their fragment."""
    text = "[excited] Gran noticia hoy. Mejora tecnologica."
    fragments = _fragment_text(text, max_chars=400)
    assert fragments[0].startswith("[excited]")


def test_concat_wav_merges_chunks() -> None:
    """_concat_wav produces a single WAV with summed frame counts."""
    first = _fake_wav(rate=8000)
    second = _fake_wav(rate=8000)
    merged = _concat_wav([first, second])
    with wave.open(io.BytesIO(merged), "rb") as reader:
        assert reader.getframerate() == 8000
        assert reader.getnframes() == 8000 * 2


def test_concat_wav_rejects_empty() -> None:
    """_concat_wav raises ValueError on an empty list."""
    with pytest.raises(ValueError):
        _concat_wav([])


@pytest.mark.asyncio
async def test_generate_tts_uses_cache(tmp_path: Path) -> None:
    """generate_tts returns cached=True when the audio file already exists."""
    service = TTSService(cache_dir=str(tmp_path))
    payload = TTSIn(markdown="hola mundo", voice="es-mx-male", language="es")

    expected_hash = _content_hash("hola mundo", "es-mx-male", "es")
    cached_file = tmp_path / f"{expected_hash}.wav"
    cached_file.write_bytes(_fake_wav())

    result = await service.generate_tts(payload)
    assert result.cached is True
    assert result.audio_path == str(cached_file)
    assert result.voice == "es-mx-male"


@pytest.mark.asyncio
async def test_generate_tts_calls_omni_voice_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """generate_tts posts to the OmniVoice API and writes the WAV."""
    service = TTSService(cache_dir=str(tmp_path))

    captured: dict[str, object] = {}

    class FakeResponse:
        def __init__(self, content: bytes) -> None:
            self.content = content

        def raise_for_status(self) -> None:
            pass

    class FakeAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def post(self, url: str, json: dict, headers: dict) -> FakeResponse:
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse(_fake_wav())

    monkeypatch.setattr("src.api.services.tts.httpx.AsyncClient", FakeAsyncClient)

    payload = TTSIn(markdown="contenido de prueba", voice="es-mx-male", language="es")
    result = await service.generate_tts(payload)

    assert result.cached is False
    assert result.audio_path.endswith(".wav")
    assert captured["url"].endswith("/api/v1/tts")
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["text"] == "contenido de prueba"
    assert body["voice_id"] == "es-mx-male"
    assert body["language"] == "es"


@pytest.mark.asyncio
async def test_generate_tts_fragments_and_concatenates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """generate_tts synthesizes each fragment and concatenates the WAVs."""
    service = TTSService(cache_dir=str(tmp_path))
    calls: list[str] = []

    async def fake_synthesize(text: str, voice: str, language: str) -> bytes:
        calls.append(text)
        return _fake_wav()

    monkeypatch.setattr(service, "_synthesize", fake_synthesize)

    long_text = "[calm] Historia uno. Detalles relevantes aqui. " + (
        "Frase extra larga para relleno y fragmentacion. " * 15
    )
    payload = TTSIn(markdown=long_text)
    result = await service.generate_tts(payload)

    assert result.cached is False
    assert len(calls) > 1
    assert result.audio_path.endswith(".wav")
    with wave.open(str(result.audio_path), "rb") as reader:
        assert reader.getnframes() == 22050 * len(calls)
