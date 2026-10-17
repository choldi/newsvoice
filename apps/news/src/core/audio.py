"""Lightweight PCM/WAV helpers shared by every audio path.

This module deliberately depends on the standard library only. The narration
service pulls in ``edge_tts`` and ``httpx`` at import time, so anything that
just needs to measure or splice raw PCM lives here instead: that keeps the
voice-design worker (which runs on a GPU box with a minimal venv) free of the
narration dependency graph.
"""

from __future__ import annotations

import array
import io
import sys
import wave
from collections.abc import Sequence
from dataclasses import dataclass


class AudioError(RuntimeError):
    """Base error for audio payload handling."""


#: Canonical narration format: what the assembly, the music mix and the M4A
#: encode all agree on. Anything else coming from an engine is resampled to it.
CANONICAL_RATE = 24000
CANONICAL_CHANNELS = 1
CANONICAL_SAMPLE_WIDTH = 2

#: Full scale of the canonical 16-bit PCM samples.
_PCM16_FULL_SCALE = 32767.0


@dataclass(frozen=True)
class WavFormat:
    """PCM format of a WAV payload.

    Attributes:
        channels: Number of audio channels.
        sample_width: Bytes per sample (2 = signed 16-bit).
        framerate: Samples per second per channel.
    """

    channels: int
    sample_width: int
    framerate: int

    @property
    def frame_size(self) -> int:
        """Return the size in bytes of one audio frame.

        Returns:
            Bytes per frame across all channels.
        """
        return self.channels * self.sample_width


CANONICAL_FORMAT = WavFormat(
    channels=CANONICAL_CHANNELS,
    sample_width=CANONICAL_SAMPLE_WIDTH,
    framerate=CANONICAL_RATE,
)


def wav_format(data: bytes) -> WavFormat:
    """Read the PCM format of a WAV payload.

    Args:
        data: Raw WAV bytes.

    Returns:
        The detected WavFormat.

    Raises:
        AudioError: If the payload is not a readable WAV.
    """
    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            return WavFormat(
                channels=handle.getnchannels(),
                sample_width=handle.getsampwidth(),
                framerate=handle.getframerate(),
            )
    except (wave.Error, EOFError) as exc:
        raise AudioError(f"invalid WAV payload: {exc}") from exc


def _data_span(data: bytes) -> tuple[int, int]:
    """Locate the frame payload of a WAV, ignoring placeholder chunk sizes.

    ffmpeg writes ``0xFFFFFFFF`` RIFF/data sizes when its output is not
    seekable, and some engines pad the header, so the declared size cannot be
    trusted: the real payload length is used instead.

    Args:
        data: Raw WAV bytes.

    Returns:
        Tuple of ``(offset, size)`` of the frame payload.

    Raises:
        AudioError: If no data chunk is found or the payload is empty.
    """
    position = data.find(b"data", 12, 80)
    if position < 0:
        raise AudioError("WAV payload has no data chunk")
    declared = int.from_bytes(data[position + 4 : position + 8], "little")
    offset = position + 8
    size = min(declared, len(data) - offset)
    if size <= 0:
        raise AudioError("WAV payload is empty")
    return offset, size


def wav_frames(data: bytes) -> bytes:
    """Return the raw PCM frames of a WAV payload (header stripped).

    Args:
        data: Raw WAV bytes.

    Returns:
        The frame bytes.

    Raises:
        AudioError: If the payload is not a readable WAV.
    """
    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            handle.readframes(0)  # validates the header
    except (wave.Error, EOFError) as exc:
        raise AudioError(f"invalid WAV payload: {exc}") from exc
    offset, size = _data_span(data)
    return data[offset : offset + size]


def build_wav(frames: bytes, fmt: WavFormat = CANONICAL_FORMAT) -> bytes:
    """Wrap raw PCM frames in a WAV container.

    Args:
        frames: Raw PCM frame bytes.
        fmt: Format of the frames. Defaults to the canonical narration format.

    Returns:
        Complete WAV bytes.
    """
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(fmt.channels)
        handle.setsampwidth(fmt.sample_width)
        handle.setframerate(fmt.framerate)
        handle.writeframes(frames)
    return buffer.getvalue()


def silence_bytes(milliseconds: int, fmt: WavFormat = CANONICAL_FORMAT) -> bytes:
    """Return PCM frames of digital silence.

    Args:
        milliseconds: Duration of the silence.
        fmt: Format of the silence. Defaults to the canonical narration format.

    Returns:
        Raw PCM frame bytes (empty when the duration is not positive).
    """
    if milliseconds <= 0:
        return b""
    samples = int(fmt.framerate * milliseconds / 1000) * fmt.channels
    return b"\x00" * (samples * fmt.sample_width)


def wav_duration(data: bytes) -> float:
    """Return the duration of a WAV payload in seconds.

    The real payload length is used, not the header's declared frame count,
    which ffmpeg leaves as a placeholder when the output is not seekable.

    Args:
        data: Raw WAV bytes.

    Returns:
        Duration in seconds, rounded to milliseconds.
    """
    fmt = wav_format(data)
    _, size = _data_span(data)
    frame_size = fmt.channels * fmt.sample_width
    return round(size / frame_size / fmt.framerate, 3)


def wav_peak_ratio(data: bytes) -> float | None:
    """Return the peak amplitude of a WAV payload as a fraction of full scale.

    Only 16-bit PCM (the canonical narration format) can be measured; any
    other sample width returns ``None`` so callers can skip a check they
    cannot evaluate instead of guessing.

    Args:
        data: Raw WAV bytes.

    Returns:
        Magnitude in ``0.0..1.0``, where ``0.0`` is digital silence, or
        ``None`` when the sample width is not 16 bit.
    """
    fmt = wav_format(data)
    if fmt.sample_width != CANONICAL_SAMPLE_WIDTH:
        return None
    frames = wav_frames(data)
    samples = array.array("h")
    samples.frombytes(frames[: len(frames) - (len(frames) % 2)])
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return 0.0
    return max(max(samples), -min(samples)) / _PCM16_FULL_SCALE


def concat_wavs(parts: Sequence[tuple[bytes, int]], fmt: WavFormat | None = None) -> bytes:
    """Concatenate WAV payloads inserting a pause after each one.

    All parts must already share the same format; the caller is responsible for
    resampling. The pause of the last element is ignored.

    Args:
        parts: Sequence of ``(wav_bytes, pause_after_ms)`` pairs.
        fmt: PCM format every part must be in. When omitted it is read from the
            first part.

    Returns:
        A single WAV payload in ``fmt``.

    Raises:
        AudioError: If the parts have mismatched formats or are empty.
    """
    if not parts:
        raise AudioError("nothing to concatenate")

    fmt = fmt or wav_format(parts[0][0])
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(fmt.channels)
        handle.setsampwidth(fmt.sample_width)
        handle.setframerate(fmt.framerate)
        for index, (data, pause_ms) in enumerate(parts):
            part_fmt = wav_format(data)
            if (
                part_fmt.channels != fmt.channels
                or part_fmt.sample_width != fmt.sample_width
                or part_fmt.framerate != fmt.framerate
            ):
                raise AudioError(f"WAV part {index} has format {part_fmt}, expected {fmt}")
            handle.writeframes(wav_frames(data))
            if index < len(parts) - 1:
                handle.writeframes(silence_bytes(pause_ms, fmt))
    return buffer.getvalue()
