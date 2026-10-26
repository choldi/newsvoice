"""Unit tests for the Phase 7.14 audio verification service."""

from __future__ import annotations

import shutil
import wave
from pathlib import Path

import numpy as np
import pytest

from src.api.services.audio_verify import (
    SAMPLE_RATE,
    AudioVerifyError,
    decode_to_samples,
    normalize_for_wer,
    respelling_delta,
    split_blocks,
    verify_audio,
)

EXPECTED = "El consejo de ministros aprobó un crecimiento del 3,5% para 2027."


class _FakeSegment:
    """Stand-in for a faster-whisper segment."""

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeInfo:
    """Stand-in for faster-whisper transcription info."""

    def __init__(self, language: str = "es") -> None:
        self.language = language


class _FakeModel:
    """WhisperModel stand-in returning one canned transcript per call."""

    def __init__(self, transcript: str, language: str = "es") -> None:
        self.transcript = transcript
        self.language = language
        self.calls: list[object] = []
        self.kwargs: list[dict] = []

    def transcribe(self, block, **kwargs):  # noqa: ANN001
        self.calls.append(block)
        self.kwargs.append(kwargs)
        return iter([_FakeSegment(self.transcript)]), _FakeInfo(self.language)


def _patch_model(monkeypatch: pytest.MonkeyPatch, model: object) -> None:
    """Replace the lazy model loader with a fake."""
    monkeypatch.setattr("src.api.services.audio_verify._load_model", lambda: model)


def _patch_decode(monkeypatch: pytest.MonkeyPatch, seconds: float = 1.0) -> None:
    """Replace ffmpeg decoding with one second of silence."""
    samples = np.zeros(int(SAMPLE_RATE * seconds), dtype=np.float32)
    monkeypatch.setattr("src.api.services.audio_verify.decode_to_samples", lambda _path: samples)


# ---------------------------------------------------------------------------
# normalize_for_wer
# ---------------------------------------------------------------------------


def test_normalize_lowercases_and_strips_punctuation() -> None:
    """Punctuation disappears; digits and letters survive."""
    assert normalize_for_wer("Hola, mundo! ¿Qué tal?") == "hola mundo qué tal"


def test_normalize_unifies_decimal_separators() -> None:
    """'3,5%' and '3.5%' must tokenize identically."""
    assert normalize_for_wer("3,5%") == normalize_for_wer("3.5%") == "3 5"


def test_normalize_drops_emotion_tags() -> None:
    """OmniVoice emotion tags are script syntax, not spoken words."""
    assert normalize_for_wer("[happy] Buenos días") == "buenos días"


def test_normalize_handles_empty_input() -> None:
    """Empty input normalizes to empty instead of raising."""
    assert normalize_for_wer("") == ""
    assert normalize_for_wer(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# respelling_delta
# ---------------------------------------------------------------------------


def test_respelling_delta_is_zero_for_identical_text() -> None:
    """No respelling means no threshold adjustment."""
    assert respelling_delta("Hola, mundo!", "hola mundo") == 0.0


def test_respelling_delta_measures_changed_words() -> None:
    """A respelled token counts as one word error."""
    assert respelling_delta("startup", "estartap") == 1.0
    delta = respelling_delta("El startup de machine learning", "El estartap de mashin lerning")
    assert 0.0 < delta <= 1.0


def test_respelling_delta_handles_empty_input() -> None:
    """Missing text cannot introduce a delta."""
    assert respelling_delta("", "estartap") == 0.0
    assert respelling_delta("startup", "") == 0.0


# ---------------------------------------------------------------------------
# split_blocks
# ---------------------------------------------------------------------------


def test_split_blocks_returns_ordered_slices() -> None:
    """A 2.5 s signal at 60 s blocks yields one short block."""
    samples = np.zeros(int(SAMPLE_RATE * 2.5), dtype=np.float32)
    blocks = split_blocks(samples, 60.0)
    assert len(blocks) == 1
    assert blocks[0].size == samples.size


def test_split_blocks_counts_full_blocks() -> None:
    """250 s at 60 s blocks yields 5 blocks (last one partial)."""
    samples = np.zeros(int(SAMPLE_RATE * 250), dtype=np.float32)
    blocks = split_blocks(samples, 60.0)
    assert len(blocks) == 5
    assert sum(block.size for block in blocks) == samples.size


def test_split_blocks_rejects_non_positive_length() -> None:
    """A zero block length would loop forever."""
    with pytest.raises(ValueError):
        split_blocks(np.zeros(10, dtype=np.float32), 0.0)


# ---------------------------------------------------------------------------
# decode_to_samples
# ---------------------------------------------------------------------------


def test_decode_missing_file_is_a_permanent_error(tmp_path: Path) -> None:
    """A missing file is reported, not raised as an OSError to the caller."""
    with pytest.raises(AudioVerifyError, match="missing"):
        decode_to_samples(str(tmp_path / "nope.wav"))


def test_decode_reads_a_real_wav(tmp_path: Path) -> None:
    """ffmpeg decodes a PCM WAV to 16 kHz mono float samples."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg missing")
    wav_path = tmp_path / "tone.wav"
    with wave.open(str(wav_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(np.zeros(8000, dtype="<i2").tobytes())

    samples = decode_to_samples(str(wav_path))

    assert samples.dtype == np.float32
    assert abs(samples.size - SAMPLE_RATE) <= SAMPLE_RATE * 0.05  # 1 s resampled


# ---------------------------------------------------------------------------
# verify_audio
# ---------------------------------------------------------------------------


async def test_verify_audio_passes_on_a_faithful_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transcript that matches the expected text scores WER 0 and passes."""
    _patch_decode(monkeypatch)
    _patch_model(monkeypatch, _FakeModel(EXPECTED))
    progress: list[tuple[int, int]] = []

    result = await verify_audio(
        "fake.mp3", EXPECTED, threshold=0.3, on_progress=lambda d, t: progress.append((d, t))
    )

    assert result.passed is True
    assert result.wer == 0.0
    assert result.verdict == "valid"
    assert result.similarity == 1.0
    assert result.language == "es"
    assert progress == [(1, 1)]


async def test_verify_audio_uses_the_requested_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller's bulletin language reaches every Whisper block."""
    _patch_decode(monkeypatch)
    model = _FakeModel(EXPECTED, language="es")
    _patch_model(monkeypatch, model)

    result = await verify_audio("fake.mp3", EXPECTED, language="es")

    assert model.kwargs[0]["language"] == "es"
    assert result.requested_language == "es"
    assert result.language == "es"


async def test_verify_audio_scores_a_secondary_reference_without_gating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The respelled text is diagnostic only; the original still decides."""
    _patch_decode(monkeypatch)
    secondary = "Noticias deportivas de última hora"
    _patch_model(monkeypatch, _FakeModel(secondary))

    result = await verify_audio(
        "fake.mp3",
        EXPECTED,
        secondary_expected_text=secondary,
        threshold=0.3,
    )

    assert result.wer_secondary == 0.0
    assert result.secondary_similarity == 1.0
    assert result.passed is False
    assert result.wer > 0.3


async def test_verify_audio_fails_a_divergent_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transcript about something else exceeds the threshold."""
    _patch_decode(monkeypatch)
    _patch_model(monkeypatch, _FakeModel("Noticias deportivas de última hora"))

    result = await verify_audio("fake.mp3", EXPECTED, threshold=0.3)

    assert result.passed is False
    assert result.verdict == "invalid"
    assert result.wer > 0.3


async def test_verify_audio_honours_a_tighter_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-call threshold overrides the global one."""
    _patch_decode(monkeypatch)
    _patch_model(monkeypatch, _FakeModel(EXPECTED + " y además otra cosa más"))

    strict = await verify_audio("fake.mp3", EXPECTED, threshold=0.0)
    assert strict.passed is False

    _patch_model(monkeypatch, _FakeModel(EXPECTED))
    relaxed = await verify_audio("fake.mp3", EXPECTED, threshold=1.0)
    assert relaxed.passed is True


async def test_verify_audio_rejects_empty_expected_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is nothing to verify against: fail before touching the model."""
    _patch_decode(monkeypatch)
    with pytest.raises(AudioVerifyError, match="expected text is empty"):
        await verify_audio("fake.mp3", "   \n  ")


async def test_verify_audio_fails_silent_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silent audio cannot match its script: WER 1.0, no jiwer crash."""
    _patch_decode(monkeypatch)
    _patch_model(monkeypatch, _FakeModel(""))

    result = await verify_audio("fake.mp3", EXPECTED, threshold=0.3)

    assert result.wer == 1.0
    assert result.passed is False
    assert result.verdict == "invalid"


async def test_verify_audio_propagates_model_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model that cannot load becomes the permanent AudioVerifyError."""
    _patch_decode(monkeypatch)

    def _boom() -> object:
        raise AudioVerifyError("cannot load Whisper model 'base'")

    monkeypatch.setattr("src.api.services.audio_verify._load_model", _boom)

    with pytest.raises(AudioVerifyError, match="cannot load"):
        await verify_audio("fake.mp3", EXPECTED)


async def test_verify_audio_transcribes_block_by_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long file is split into blocks and every block reaches the model."""
    _patch_decode(monkeypatch, seconds=130.0)  # 130 s at 60 s blocks = 3 blocks
    model = _FakeModel("hola mundo")
    _patch_model(monkeypatch, model)
    progress: list[tuple[int, int]] = []

    result = await verify_audio(
        "fake.mp3",
        "hola mundo hola mundo hola mundo",
        on_progress=lambda d, t: progress.append((d, t)),
    )

    assert len(model.calls) == 3
    assert result.block_count == 3
    assert result.blocks == ["hola mundo"] * 3
    assert progress == [(1, 3), (2, 3), (3, 3)]
    assert result.duration_seconds == pytest.approx(130.0, abs=0.1)
