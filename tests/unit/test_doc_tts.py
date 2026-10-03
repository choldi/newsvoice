"""Unit tests for the Phase 7.11 document TTS service (chunking, cache, meta)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.api.services.doc_tts import (
    DocTTSService,
    chunk_speech_text,
    job_meta_path,
    job_text_path,
    read_job_meta,
    slugify,
    update_job_meta,
    write_job_meta,
)


def _reset_settings(monkeypatch: pytest.MonkeyPatch, tmp_path, **overrides: object) -> None:
    """Point the doc-tts settings at a temporary directory."""
    monkeypatch.setattr("src.core.config.settings.doc_tts_dir", str(tmp_path / "doc_tts"))
    monkeypatch.setattr("src.core.config.settings.doc_tts_chunk_chars", 60)
    monkeypatch.setattr("src.core.config.settings.doc_tts_concurrency", 2)
    monkeypatch.setattr("src.core.config.settings.tts_language", "es")
    monkeypatch.setattr("src.core.config.settings.tts_voice", "es-ES-ElviraNeural")
    for key, value in overrides.items():
        monkeypatch.setattr(f"src.core.config.settings.{key}", value)


def _mock_voice(monkeypatch: pytest.MonkeyPatch, voice: str = "es-ES-ElviraNeural") -> AsyncMock:
    """Replace voice resolution so no network catalogue is needed."""
    mock = AsyncMock(return_value=voice)
    monkeypatch.setattr("src.api.services.doc_tts.resolve_edge_voice", mock)
    return mock


def _mock_synth(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace edge-tts with a fake that marks every chunk it receives."""

    async def fake(text: str, voice: str, *, timeout: int | None = None) -> bytes:
        # 40 chars keep each chunk distinguishable in the joined bytes.
        return f"<{text[:40]}>".encode()

    mock = AsyncMock(side_effect=fake)
    monkeypatch.setattr("src.api.services.doc_tts.synthesize_mp3", mock)
    return mock


# ---------------------------------------------------------------------------
# chunk_speech_text
# ---------------------------------------------------------------------------


def test_chunk_empty_input_returns_no_chunks() -> None:
    """Blank input produces no work instead of one empty chunk."""
    assert chunk_speech_text("   \n  ", 100) == []
    assert chunk_speech_text("", 100) == []


def test_chunk_short_text_is_a_single_chunk() -> None:
    """Text under the limit is not split."""
    text = "Hola mundo, esto es una frase corta."
    assert chunk_speech_text(text, 100) == [text]


def test_chunk_prefers_paragraph_boundaries() -> None:
    """Paragraphs stay intact when they fit in a chunk."""
    paragraphs = [
        f"Parrafo numero {i} con algo de texto para completar la linea." for i in range(6)
    ]
    text = "\n\n".join(paragraphs)
    chunks = chunk_speech_text(text, 80)

    assert len(chunks) > 1
    assert all(len(chunk) <= 80 for chunk in chunks)
    assert chunks[0].startswith("Parrafo numero 0")
    # Every paragraph survives whole (never cut in the middle).
    for paragraph in paragraphs:
        assert paragraph in chunks


def test_chunk_falls_back_to_sentence_then_word_boundaries() -> None:
    """Without paragraphs, sentences (then words) are the cut points."""
    sentence_text = "Primera frase larga. Segunda frase larga. Tercera frase larga. Cuarta frase."
    chunks = chunk_speech_text(sentence_text, 40)
    assert all(len(chunk) <= 40 for chunk in chunks)
    assert all(chunk.endswith(".") for chunk in chunks)

    word_text = "uno dos tres cuatro cinco seis siete ocho nueve diez once doce"
    chunks = chunk_speech_text(word_text, 20)
    assert all(len(chunk) <= 20 for chunk in chunks)
    # Word boundaries: no chunk may end in the middle of a word.
    assert all(not chunk.endswith(" ") for chunk in chunks)


def test_chunk_hard_cuts_a_single_oversized_token() -> None:
    """A token longer than the limit is cut rather than looping forever."""
    token = "a" * 250
    chunks = chunk_speech_text(token, 100)
    assert "".join(chunks) == token
    assert all(len(chunk) <= 100 for chunk in chunks)


def test_chunk_loses_no_words() -> None:
    """Joining the chunks back yields exactly the input's words, in order."""
    text = (
        "El consejo de ministros aprobo el presupuesto con un 3,5% de crecimiento. "
        "La oposicion pidio una revision de los datos del INE.\n\n"
        "El ministro afirmo que las cuentas cerraran en diciembre."
    )
    chunks = chunk_speech_text(text, 45)
    assert len(chunks) > 2
    assert " ".join(" ".join(chunks).split()) == " ".join(text.split())


def test_chunk_rejects_a_nonpositive_limit() -> None:
    """A limit below 1 is a programming error, not an empty result."""
    with pytest.raises(ValueError):
        chunk_speech_text("hola", 0)


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


def test_slugify_normalizes_titles() -> None:
    """Accents and punctuation collapse into a safe stem."""
    assert slugify("El nuevo Gobierno: ¿presupuesto 2027?") == "el-nuevo-gobierno-presupuesto-2027"


def test_slugify_falls_back_for_unusable_titles() -> None:
    """Titles without alphanumerics still produce a usable filename part."""
    assert slugify("???") == "documento"
    assert slugify("") == "documento"


def test_slugify_caps_length() -> None:
    """Very long titles do not create unwieldy filenames."""
    assert len(slugify("palabra " * 50)) <= 40


# ---------------------------------------------------------------------------
# synthesize_document
# ---------------------------------------------------------------------------


async def test_synthesize_creates_audio_and_reports_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The finished MP3 exists, chunks are spoken in order, progress is monotonic."""
    _reset_settings(monkeypatch, tmp_path)
    _mock_voice(monkeypatch)
    synth = _mock_synth(monkeypatch)
    text = "\n\n".join(f"Oracion numero {i} con texto suficiente para el limite." for i in range(6))

    progress: list[tuple[int, int]] = []
    service = DocTTSService()
    result = await service.synthesize_document(
        text=text,
        title="Mi documento",
        on_progress=lambda done, total: progress.append((done, total)),
    )

    assert result.cached is False
    assert result.chunk_count > 1
    assert synth.await_count == result.chunk_count
    assert [done for done, _ in progress] == list(range(1, result.chunk_count + 1))
    assert {total for _, total in progress} == {result.chunk_count}

    from pathlib import Path

    audio = Path(result.audio_path)
    assert audio.exists()
    assert audio.with_suffix(".meta.json").exists()
    # Chunks are joined in order: the fake marks each chunk with its opening.
    content = audio.read_bytes()
    assert content.startswith(b"<Oracion num")
    assert content.index(b"numero 5") > content.index(b"numero 0")


async def test_synthesize_second_call_hits_the_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Same text + voice + language reuses the finished file."""
    _reset_settings(monkeypatch, tmp_path)
    _mock_voice(monkeypatch)
    synth = _mock_synth(monkeypatch)
    text = "Texto estable que no cambia entre llamadas."

    service = DocTTSService()
    first = await service.synthesize_document(text=text, title="Doc")
    second = await service.synthesize_document(text=text, title="Doc")

    assert first.cached is False
    assert second.cached is True
    assert second.audio_path == first.audio_path
    assert synth.await_count == 1, "a cache hit must not touch the engine"


async def test_synthesize_reuses_chunks_when_the_final_file_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Deleting the joined file still leaves every chunk cached for the retry."""
    _reset_settings(monkeypatch, tmp_path)
    _mock_voice(monkeypatch)
    synth = _mock_synth(monkeypatch)
    text = "\n\n".join(f"Parrafo {i} con frases para poblar la cache de trozos." for i in range(4))

    service = DocTTSService()
    first = await service.synthesize_document(text=text, title="Doc")
    from pathlib import Path as _Path

    _Path(first.audio_path).unlink()

    second = await service.synthesize_document(text=text, title="Doc")

    assert second.cached is False
    assert synth.await_count == first.chunk_count, "cached chunks must not be re-spoken"
    assert _Path(second.audio_path).exists()


async def test_synthesize_changed_voice_invalidates_the_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The cache key covers the voice: another voice means new audio."""
    _reset_settings(monkeypatch, tmp_path)
    _mock_voice(monkeypatch, "es-ES-ElviraNeural")
    synth = _mock_synth(monkeypatch)
    text = "El mismo texto con dos voces distintas."

    service = DocTTSService()
    first = await service.synthesize_document(text=text, title="Doc")
    _mock_voice(monkeypatch, "es-ES-AlvaroNeural")
    second = await service.synthesize_document(text=text, title="Doc")

    assert second.voice == "es-ES-AlvaroNeural"
    assert second.audio_path != first.audio_path
    assert synth.await_count == 2


async def test_synthesize_retries_a_flaky_chunk(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A chunk that fails twice is retried, not failed over the whole book."""
    _reset_settings(monkeypatch, tmp_path)
    _mock_voice(monkeypatch)
    calls: list[str] = []

    async def flaky(text: str, voice: str, *, timeout: int | None = None) -> bytes:
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("edge flaked")
        return b"MP3"

    monkeypatch.setattr("src.api.services.doc_tts.synthesize_mp3", AsyncMock(side_effect=flaky))
    monkeypatch.setattr("src.api.services.doc_tts.asyncio.sleep", AsyncMock())

    result = await DocTTSService().synthesize_document(text="Una sola frase corta.", title="Doc")

    assert result.chunk_count == 1
    assert len(calls) == 2


async def test_synthesize_fails_after_all_attempts(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A permanently broken engine surfaces as an error with the attempt count."""
    _reset_settings(monkeypatch, tmp_path)
    _mock_voice(monkeypatch)
    monkeypatch.setattr(
        "src.api.services.doc_tts.synthesize_mp3",
        AsyncMock(side_effect=RuntimeError("no audio")),
    )

    with pytest.raises(RuntimeError, match="after 3 attempts"):
        await DocTTSService().synthesize_document(text="Texto.", title="Doc")


# ---------------------------------------------------------------------------
# job metadata helpers
# ---------------------------------------------------------------------------


def test_job_meta_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Metadata merges across writes (submit -> extract -> synthesize)."""
    _reset_settings(monkeypatch, tmp_path)
    write_job_meta("doc-tts-abc123", {"title": "Mi libro", "filename": "libro.pdf"})
    update_job_meta("doc-tts-abc123", char_count=1234, kind="pdf")

    meta = read_job_meta("doc-tts-abc123")
    assert meta["title"] == "Mi libro"
    assert meta["char_count"] == 1234
    assert meta["kind"] == "pdf"


def test_job_paths_reject_foreign_ids(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Ids become file paths: traversal attempts are refused."""
    _reset_settings(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="invalid document job id"):
        job_meta_path("../../etc/passwd")
    with pytest.raises(ValueError, match="invalid document job id"):
        job_text_path("..%2Fx")
    assert read_job_meta("../../etc/passwd") == {}


def test_job_paths_use_the_configured_directory(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Every job artifact lives under the configured doc_tts_dir."""
    _reset_settings(monkeypatch, tmp_path)
    meta = job_meta_path("doc-tts-abc123")
    text = job_text_path("doc-tts-abc123")
    assert str(meta).startswith(str(tmp_path))
    assert meta.name == "doc-tts-abc123.json"
    assert text.name == "doc-tts-abc123.txt"
