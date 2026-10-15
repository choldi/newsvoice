"""Audio verification with local Whisper (Phase 7.14).

Transcribes the audio that is about to be delivered and compares it against
the text that produced it, so a render that went off-script (hallucinated
repetition, clipped audio, wrong voice) never reaches a channel. The score is
the word error rate (WER, jiwer): **lower is better** and ``passed`` means
``wer <= threshold``.

The model runs on CPU via faster-whisper/CTranslate2; the audio is decoded to
16 kHz mono PCM with ffmpeg and transcribed in fixed-length blocks so memory
stays bounded and the activity can heartbeat between blocks.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import structlog

from src.core.config import get_settings
from src.core.prompts import strip_emotion_tags
from src.core.tracing import trace_function

logger = structlog.get_logger(__name__)

SAMPLE_RATE = 16_000
DECODE_TIMEOUT_SECONDS = 300
# Punctuation and separators are dropped so "3,5%" (script) and "3.5%" or
# "3 5" (ASR) tokenize identically; digits survive untouched.
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


class AudioVerifyError(Exception):
    """A permanent failure: no audio, no expected text, unusable file."""


@dataclass
class AudioVerification:
    """Outcome of one audio verification."""

    wer: float
    similarity: float
    passed: bool
    threshold: float
    transcript: str
    blocks: list[str] = field(default_factory=list)
    block_count: int = 0
    duration_seconds: float = 0.0
    model: str = ""
    language: str = ""
    expected_chars: int = 0
    transcript_chars: int = 0
    verdict: str = "valid"

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict (details column of verifications).

        Returns:
            All fields as plain Python types.
        """
        return {
            "wer": self.wer,
            "similarity": self.similarity,
            "passed": self.passed,
            "threshold": self.threshold,
            "transcript": self.transcript,
            "blocks": self.blocks,
            "block_count": self.block_count,
            "duration_seconds": self.duration_seconds,
            "model": self.model,
            "language": self.language,
            "expected_chars": self.expected_chars,
            "transcript_chars": self.transcript_chars,
        }


def normalize_for_wer(text: str) -> str:
    """Normalize text so ASR output and script compare fairly.

    Lowercases, drops emotion tags and every non-word character (punctuation,
    percent signs, decimal separators all become separators) and collapses
    whitespace.

    Args:
        text: Raw script or transcript.

    Returns:
        Normalized whitespace-separated text.
    """
    cleaned = strip_emotion_tags(text or "")
    cleaned = _NON_WORD_RE.sub(" ", cleaned.lower())
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def decode_to_samples(audio_path: str) -> np.ndarray:
    """Decode any audio file to 16 kHz mono float32 PCM with ffmpeg.

    Args:
        audio_path: Path of the WAV/M4A/MP3 to decode.

    Returns:
        Mono float32 samples at 16 kHz.

    Raises:
        AudioVerifyError: When the file is missing or ffmpeg fails.
    """
    path = Path(audio_path)
    if not path.is_file():
        raise AudioVerifyError(f"audio file is missing: {audio_path}")
    command = [
        str(get_settings().ffmpeg_path),
        "-v",
        "error",
        "-i",
        str(path),
        "-f",
        "f32le",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "1",
        "pipe:1",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=DECODE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioVerifyError(f"ffmpeg could not decode {audio_path}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()[-300:]
        raise AudioVerifyError(f"ffmpeg failed on {audio_path}: {detail}")
    samples = np.frombuffer(result.stdout, dtype="<f4").copy()
    if samples.size == 0:
        raise AudioVerifyError(f"audio file has no samples: {audio_path}")
    return samples


def split_blocks(samples: np.ndarray, block_seconds: float) -> list[np.ndarray]:
    """Split PCM into fixed-length blocks (the final one may be shorter).

    Args:
        samples: Mono 16 kHz float32 samples.
        block_seconds: Nominal block length in seconds.

    Returns:
        Non-empty blocks in order.

    Raises:
        ValueError: When block_seconds is not positive.
    """
    if block_seconds <= 0:
        raise ValueError("block_seconds must be > 0")
    per_block = int(SAMPLE_RATE * block_seconds)
    return [samples[i : i + per_block] for i in range(0, samples.size, per_block)]


# One model per (size, device, compute type, directory): loading a model costs
# seconds and downloading it costs ~150 MB, so every activity in this process
# shares it. Guarded by a lock because activities run concurrently.
_MODEL_LOCK = threading.Lock()
_MODELS: dict[tuple[str, str, str, str], object] = {}


def _load_model() -> object:
    """Return the cached faster-whisper model, downloading it on first use.

    Returns:
        The shared WhisperModel instance.

    Raises:
        AudioVerifyError: When the model cannot be loaded or downloaded.
    """
    settings = get_settings()
    key = (
        settings.audio_verify_model,
        settings.audio_verify_device,
        settings.audio_verify_compute_type,
        settings.audio_verify_model_dir,
    )
    with _MODEL_LOCK:
        model = _MODELS.get(key)
        if model is not None:
            return model
        try:
            from faster_whisper import WhisperModel

            model = WhisperModel(
                settings.audio_verify_model,
                device=settings.audio_verify_device,
                compute_type=settings.audio_verify_compute_type,
                download_root=settings.audio_verify_model_dir,
            )
        except Exception as exc:
            raise AudioVerifyError(
                f"cannot load Whisper model {settings.audio_verify_model!r}: {exc}"
            ) from exc
        logger.info("whisper_model_loaded", model=settings.audio_verify_model)
        _MODELS[key] = model
        return model


def _transcribe_block(model: object, block: np.ndarray, language: str) -> tuple[str, str]:
    """Transcribe one block; runs inside a worker thread.

    Args:
        model: Loaded WhisperModel.
        block: Mono 16 kHz samples of the block.
        language: ISO 639-1 hint, or '' to auto-detect.

    Returns:
        Tuple of (block transcript, detected/used language).
    """
    segments, info = model.transcribe(  # type: ignore[attr-defined]
        block,
        language=language or None,
        # Every block starts fresh: a repetition loop at a block boundary must
        # not poison the whole transcription.
        condition_on_previous_text=False,
        beam_size=5,
    )
    text = " ".join(segment.text.strip() for segment in segments)
    detected = getattr(info, "language", "") or language
    return text.strip(), str(detected)


@trace_function(component="api.services.audio_verify")
async def verify_audio(
    audio_path: str,
    expected_text: str,
    *,
    threshold: float | None = None,
    on_progress: object | None = None,
) -> AudioVerification:
    """Transcribe audio and score it against the text that produced it.

    The whole pipeline (ffmpeg decode, model load, transcription) runs in
    worker threads: faster-whisper is synchronous CPU work and must never
    stall the activity's event loop.

    Args:
        audio_path: File to verify (WAV, M4A, MP3...).
        expected_text: What the audio is supposed to say.
        threshold: Maximum accepted WER; defaults to
            ``audio_verify_wer_threshold``.
        on_progress: Callable ``(done, total)`` invoked after each block.

    Returns:
        The verification with score, transcript and per-block detail.

    Raises:
        AudioVerifyError: When there is no expected text, no samples, or the
            model cannot run (the caller records a 'error' verdict).
    """
    settings = get_settings()
    expected = normalize_for_wer(expected_text)
    if not expected:
        raise AudioVerifyError("expected text is empty: nothing to verify against")
    limit = settings.audio_verify_wer_threshold if threshold is None else float(threshold)

    samples = await asyncio.to_thread(decode_to_samples, audio_path)
    blocks = split_blocks(samples, settings.audio_verify_block_seconds)
    duration = round(samples.size / SAMPLE_RATE, 2)

    model = await asyncio.to_thread(_load_model)
    transcripts: list[str] = []
    detected = ""
    total = len(blocks)
    for index, block in enumerate(blocks, start=1):
        text, language = await asyncio.to_thread(
            _transcribe_block, model, block, settings.audio_verify_language
        )
        transcripts.append(text)
        if not detected:
            detected = language
        if on_progress is not None:
            on_progress(index, total)  # type: ignore[operator]

    transcript = " ".join(part for part in transcripts if part).strip()
    hypothesis = normalize_for_wer(transcript)
    if not hypothesis:
        # Silent audio cannot possibly match its script: fail it explicitly
        # instead of letting jiwer reject an empty hypothesis.
        wer = 1.0
    else:
        import jiwer

        wer = float(jiwer.wer(expected, hypothesis))
    wer = max(0.0, min(wer, 1.0))
    passed = wer <= limit
    logger.info(
        "audio_verified",
        audio_path=audio_path,
        wer=round(wer, 4),
        threshold=limit,
        passed=passed,
        blocks=total,
        duration_seconds=duration,
    )
    return AudioVerification(
        wer=wer,
        similarity=round(1.0 - wer, 4),
        passed=passed,
        threshold=limit,
        transcript=transcript,
        blocks=transcripts,
        block_count=total,
        duration_seconds=duration,
        model=settings.audio_verify_model,
        language=detected,
        expected_chars=len(expected),
        transcript_chars=len(hypothesis),
        verdict="valid" if passed else "invalid",
    )
