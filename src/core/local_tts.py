"""Local Qwen3-TTS VoiceDesign backend (Phase 7.9).

Runs on the GPU box instead of Modal. The model is loaded once per process and
reused across a batch of voices, so designing the whole catalog costs a single
cold start.

Imported lazily by the voice-design activity, so neither the local narration
path nor the test suite needs ``qwen_tts``, ``torch`` or ``numpy``: the audio
helpers below work on plain sequences, and the heavy imports happen only inside
:func:`get_backend`.
"""

from __future__ import annotations

import array
import threading
from typing import Any

from src.core.audio import (
    CANONICAL_FORMAT,
    CANONICAL_RATE,
    WavFormat,
    build_wav,
    concat_wavs,
)
from src.core.config import settings

#: Qwen3-TTS expects full lowercase language names, not ISO 639-1 codes.
QWEN_LANGUAGE_NAMES: dict[str, str] = {
    "en": "english",
    "es": "spanish",
    "fr": "french",
    "ca": "french",
    "pt": "portuguese",
    "de": "german",
    "it": "italian",
    "ja": "japanese",
    "ko": "korean",
    "ru": "russian",
}

#: Silence inserted between reference segments so the join is not audible.
SEGMENT_GAP_MS = 200

#: Scale factor for float audio in [-1.0, 1.0] to signed 16-bit PCM.
_FLOAT_TO_PCM16 = 32767.0

#: Clamp for signed 16-bit PCM samples.
_PCM16_MIN = -32768
_PCM16_MAX = 32767


class LocalTTSError(RuntimeError):
    """Raised when the local TTS backend cannot produce audio."""


def resolve_language(language: str) -> str:
    """Map an ISO 639-1 code to the name Qwen3-TTS expects.

    Args:
        language: ISO 639-1 code.

    Returns:
        The Qwen3-TTS language name.

    Raises:
        LocalTTSError: If the code has no mapping.
    """
    try:
        return QWEN_LANGUAGE_NAMES[language]
    except KeyError:
        raise LocalTTSError(f"no Qwen3-TTS language mapping for {language!r}") from None


def _looks_like_float(samples: list[Any]) -> bool:
    """Report whether the leading samples are floating-point audio.

    Works for both Python floats and numpy floating scalars, which are not
    ``float`` subclasses in current numpy.

    Args:
        samples: Audio samples to inspect.

    Returns:
        True when the samples should be treated as float in [-1.0, 1.0].
    """
    for value in samples[:4]:
        if isinstance(value, float):
            return True
        dtype = getattr(value, "dtype", None)
        if dtype is not None and str(dtype).startswith("float"):
            return True
    return False


def _to_pcm16(value: float) -> int:
    """Clamp a scaled sample to the signed 16-bit range.

    Args:
        value: Sample already scaled to PCM range.

    Returns:
        The clamped integer sample.
    """
    return max(_PCM16_MIN, min(_PCM16_MAX, int(round(value))))


def to_wav_bytes(audio: Any, sample_rate: int) -> bytes:
    """Convert audio samples to 16-bit PCM WAV bytes.

    Args:
        audio: Audio as bytes, an ``array.array``, or a sequence of float samples
            in [-1.0, 1.0] / integer samples.
        sample_rate: Sample rate of ``audio``.

    Returns:
        WAV bytes at ``sample_rate``.

    Raises:
        LocalTTSError: If the audio is empty.
    """
    if isinstance(audio, (bytes, bytearray)):
        return build_wav(bytes(audio), WavFormat(1, 2, sample_rate))
    samples = list(audio)
    if not samples:
        raise LocalTTSError("the TTS backend returned empty audio")
    fmt = WavFormat(1, 2, sample_rate)
    if _looks_like_float(samples):
        frames = array.array("h", (_to_pcm16(float(s) * _FLOAT_TO_PCM16) for s in samples))
        return build_wav(frames.tobytes(), fmt)
    return build_wav(array.array("h", samples).tobytes(), fmt)


def resample(audio: Any, from_rate: int, to_rate: int) -> Any:
    """Resample mono audio by linear interpolation.

    The sample representation is preserved: float audio in [-1.0, 1.0] stays
    float, integer PCM stays integer, so :func:`to_wav_bytes` still knows how
    to scale it.

    Args:
        audio: Audio samples.
        from_rate: Current sample rate.
        to_rate: Target sample rate.

    Returns:
        The resampled samples, untouched when the rates already match.
    """
    if from_rate == to_rate:
        return audio
    samples = list(audio)
    target_length = int(round(len(samples) / from_rate * to_rate))
    if target_length <= 0:
        return samples
    keep_float = _looks_like_float(samples)
    frames: array.array = array.array("f" if keep_float else "h")
    last = len(samples) - 1
    for index in range(target_length):
        position = index * last / (target_length - 1) if target_length > 1 else 0.0
        low = int(position)
        high = min(low + 1, last)
        fraction = position - low
        value = float(samples[low]) + (float(samples[high]) - float(samples[low])) * fraction
        frames.append(value if keep_float else _to_pcm16(value))
    return frames


class QwenVoiceDesign:
    """Lazy singleton wrapper around the Qwen3-TTS VoiceDesign model.

    Attributes:
        supported_languages: Language names the loaded model accepts.
    """

    def __init__(self, model: Any, supported_languages: list[str]) -> None:
        """Initialize the wrapper.

        Args:
            model: A loaded ``Qwen3TTSModel``.
            supported_languages: Language names the model accepts.
        """
        self._model = model
        self._supported_languages = supported_languages

    @property
    def supported_languages(self) -> list[str]:
        """Return the language names the loaded model accepts."""
        return list(self._supported_languages)

    def generate(
        self,
        segments: list[str],
        instruct: str,
        language: str,
    ) -> tuple[bytes, int]:
        """Synthesize each segment in the designed voice and concatenate them.

        Args:
            segments: Reference text segments, spoken in order.
            instruct: VoiceDesign persona description.
            language: ISO 639-1 code.

        Returns:
            Tuple of ``(wav_bytes, sample_rate)`` in the canonical format.

        Raises:
            LocalTTSError: On any failure.
        """
        if not segments:
            raise LocalTTSError("at least one reference segment is required")
        qwen_language = resolve_language(language)
        try:
            parts: list[tuple[bytes, int]] = []
            for text in segments:
                wavs, sample_rate = self._model.generate_voice_design(
                    text=text,
                    instruct=instruct,
                    language=qwen_language,
                )
                audio = resample(wavs[0], sample_rate, CANONICAL_RATE)
                parts.append((to_wav_bytes(audio, CANONICAL_RATE), SEGMENT_GAP_MS))
            return concat_wavs(parts, CANONICAL_FORMAT), CANONICAL_RATE
        except LocalTTSError:
            raise
        except Exception as exc:
            raise LocalTTSError(f"voice design failed: {exc}") from exc


_BACKEND: QwenVoiceDesign | None = None
_BACKEND_LOCK = threading.Lock()


def _load_backend() -> QwenVoiceDesign:
    """Load the Qwen3-TTS VoiceDesign model.

    Returns:
        The wrapped model.

    Raises:
        LocalTTSError: If the model cannot be loaded.
    """
    try:
        import torch
        from qwen_tts import Qwen3TTSModel

        model = Qwen3TTSModel.from_pretrained(
            settings.qwen_tts_design_model,
            device_map=settings.qwen_tts_device,
            dtype=torch.bfloat16,
            attn_implementation=settings.qwen_tts_attn_implementation,
        )
        return QwenVoiceDesign(model, model.get_supported_languages())
    except Exception as exc:
        raise LocalTTSError(f"could not load the VoiceDesign model: {exc}") from exc


def get_backend() -> QwenVoiceDesign:
    """Return the process-wide VoiceDesign backend, loading it on first use.

    Returns:
        The shared backend.

    Raises:
        LocalTTSError: If the model cannot be loaded.
    """
    global _BACKEND
    if _BACKEND is None:
        with _BACKEND_LOCK:
            if _BACKEND is None:
                _BACKEND = _load_backend()
    return _BACKEND


def design_voice(
    segments: list[str],
    instruct: str,
    language: str,
) -> tuple[bytes, int]:
    """Design a voice by synthesizing reference segments locally.

    Args:
        segments: Reference text segments, spoken in order.
        instruct: VoiceDesign persona description.
        language: ISO 639-1 code.

    Returns:
        Tuple of ``(wav_bytes, sample_rate)`` in the canonical format.

    Raises:
        LocalTTSError: On any failure.
    """
    return get_backend().generate(segments, instruct, language)
