"""Unit tests for the local Qwen3-TTS VoiceDesign backend (Phase 7.9)."""

from __future__ import annotations

import array

import pytest

from src.core import local_tts
from src.core.audio import CANONICAL_RATE, wav_duration, wav_peak_ratio
from src.core.local_tts import (
    SEGMENT_GAP_MS,
    LocalTTSError,
    QwenVoiceDesign,
    design_voice,
    get_backend,
    resample,
    resolve_language,
    to_wav_bytes,
)


class _FakeModel:
    """Stand-in for ``Qwen3TTSModel`` that returns canned audio."""

    def __init__(self, audio: list[float], sample_rate: int = CANONICAL_RATE) -> None:
        self._audio = audio
        self._sample_rate = sample_rate
        self.calls: list[dict[str, str]] = []

    def get_supported_languages(self) -> list[str]:
        return ["english", "spanish"]

    def generate_voice_design(
        self, text: str, instruct: str, language: str
    ) -> tuple[list[list[float]], int]:
        self.calls.append({"text": text, "instruct": instruct, "language": language})
        return [list(self._audio)], self._sample_rate


def _backend(model: _FakeModel) -> QwenVoiceDesign:
    return QwenVoiceDesign(model, model.get_supported_languages())


# --- language resolution ----------------------------------------------------


def test_resolve_language_maps_iso_to_qwen_name() -> None:
    assert resolve_language("en") == "english"
    assert resolve_language("es") == "spanish"


def test_resolve_language_rejects_unknown_code() -> None:
    with pytest.raises(LocalTTSError, match="no Qwen3-TTS language mapping"):
        resolve_language("fr")


# --- audio helpers ----------------------------------------------------------


def test_to_wav_bytes_converts_float_audio() -> None:
    wav = to_wav_bytes([0.0, 1.0, -1.0, 0.5], 24000)
    assert wav_duration(wav) == round(4 / 24000, 3)


def test_to_wav_bytes_passes_raw_frames_through() -> None:
    frames = array.array("h", [0, 100, -100])
    assert to_wav_bytes(frames, 24000) == to_wav_bytes(list(frames), 24000)


def test_to_wav_bytes_rejects_empty_audio() -> None:
    with pytest.raises(LocalTTSError, match="empty audio"):
        to_wav_bytes([], 24000)


def test_resample_is_a_noop_when_rates_match() -> None:
    audio = array.array("h", [0, 100, 200])
    assert list(resample(audio, 24000, 24000)) == [0, 100, 200]


def test_resample_upsamples_to_the_target_length() -> None:
    resampled = resample([0.0, 100.0], 24000, 48000)

    assert len(resampled) == 4
    assert resampled[0] == 0.0
    assert resampled[-1] == 100.0
    assert resampled[1] < resampled[2] < resampled[3]


def test_resample_downsamples_to_the_target_length() -> None:
    audio = [0.0, 50.0, 100.0, 150.0]
    resampled = resample(audio, 48000, 24000)
    assert len(resampled) == 2


def test_resample_keeps_float_audio_in_float_range() -> None:
    resampled = resample([0.5, 1.0], 24000, 48000)

    assert max(resampled) <= 1.0
    assert min(resampled) >= 0.5


# --- generate ---------------------------------------------------------------


def test_generate_concatenates_segments_with_a_gap() -> None:
    model = _FakeModel([0.0] * 2400)
    wav, sample_rate = _backend(model).generate(
        ["first segment", "second segment"], "a warm voice", "en"
    )

    assert sample_rate == CANONICAL_RATE
    assert wav_duration(wav) == round((2400 * 2 + SEGMENT_GAP_MS * 24) / 24000, 3)
    assert model.calls == [
        {"text": "first segment", "instruct": "a warm voice", "language": "english"},
        {"text": "second segment", "instruct": "a warm voice", "language": "english"},
    ]


def test_generate_resamples_a_foreign_sample_rate() -> None:
    model = _FakeModel([0.5] * 4800, sample_rate=48000)
    wav, sample_rate = _backend(model).generate(["text"], "instruct", "es")

    assert sample_rate == CANONICAL_RATE
    assert wav_duration(wav) == round(4800 / 48000, 3)
    assert wav_peak_ratio(wav) == pytest.approx(0.5, abs=0.01)


def test_generate_rejects_empty_segments() -> None:
    with pytest.raises(LocalTTSError, match="at least one reference segment"):
        _backend(_FakeModel([0.0])).generate([], "instruct", "en")


def test_generate_rejects_unsupported_language() -> None:
    with pytest.raises(LocalTTSError, match="no Qwen3-TTS language mapping"):
        _backend(_FakeModel([0.0])).generate(["text"], "instruct", "fr")


def test_generate_wraps_model_errors() -> None:
    class _Broken:
        def get_supported_languages(self) -> list[str]:
            return ["english"]

        def generate_voice_design(self, **kwargs: object) -> None:
            raise RuntimeError("cuda out of memory")

    with pytest.raises(LocalTTSError, match="voice design failed"):
        _backend(_Broken()).generate(["text"], "instruct", "en")


# --- singleton --------------------------------------------------------------


def test_get_backend_is_a_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_tts, "_BACKEND", None)
    sentinel = object()
    monkeypatch.setattr(local_tts, "_load_backend", lambda: sentinel)

    assert get_backend() is sentinel
    assert get_backend() is sentinel


def test_design_voice_delegates_to_the_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _FakeModel([0.0] * 2400)
    monkeypatch.setattr(local_tts, "get_backend", lambda: _backend(model))

    wav, sample_rate = design_voice(["hello"], "instruct", "en")

    assert sample_rate == CANONICAL_RATE
    assert wav_duration(wav) == round(2400 / 24000, 3)
