"""Audio compression service: transcode the synthesized WAV to M4A (AAC).

The OmniVoice backend renders long digests into large WAV files that can exceed
Telegram's 50 MB upload limit and are heavy for email attachments. Before
delivery, the audio is transcoded to a mono AAC M4A (audio/mp4) file, a format
playable on Telegram, WhatsApp, Android, and iPhone.
"""

import asyncio
import os
import subprocess
from pathlib import Path

import structlog

from src.core.config import get_settings
from src.core.tracing import trace_function

logger = structlog.get_logger(__name__)


class CompressAudioError(RuntimeError):
    """Raised when ffmpeg is unavailable or the transcoding fails."""


class AudioCompressService:
    """Transcode a WAV file to an M4A (AAC) file using ffmpeg."""

    def __init__(self, ffmpeg_path: str | None = None, bitrate_kbps: int | None = None) -> None:
        """Initialize with optional overrides for the ffmpeg binary and bitrate.

        Args:
            ffmpeg_path: Path to the ffmpeg binary. Defaults to settings.ffmpeg_path.
            bitrate_kbps: AAC bitrate in kbps. Defaults to settings.tts_audio_bitrate_kbps.
        """
        settings = get_settings()
        self._ffmpeg = ffmpeg_path or settings.ffmpeg_path
        self._bitrate = bitrate_kbps or settings.tts_audio_bitrate_kbps

    @trace_function(component="api.services.audio_compress")
    async def compress(self, source_path: str) -> str:
        """Transcode a WAV file to a mono AAC M4A placed next to the source.

        The target M4A is reused only when it is not older than its source, so a
        regenerated MP3 is never delivered with the previous audio. The
        transcode writes to a temporary file and is renamed into place, so a
        failed run cannot leave a truncated M4A behind.

        Args:
            source_path: Path to the WAV file to compress.

        Returns:
            Path to the compressed .m4a file.

        Raises:
            CompressAudioError: If the source is missing, ffmpeg is not found,
                the command fails, or the expected output is not produced.
        """
        source = Path(source_path)
        if not source.exists():
            raise CompressAudioError(f"Audio file not found: {source_path}")

        target = source.with_suffix(".m4a")
        if target.exists() and target.stat().st_mtime_ns >= source.stat().st_mtime_ns:
            logger.info("audio_compress_cached", source=str(source), target=str(target))
            return str(target)

        tmp_target = source.with_name(f"{source.stem}.tmp.m4a")
        command = [
            self._ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-c:a",
            "aac",
            "-b:a",
            f"{self._bitrate}k",
            "-movflags",
            "+faststart",
            str(tmp_target),
        ]
        try:
            result = await asyncio.to_thread(
                subprocess.run, command, capture_output=True, text=True, timeout=300
            )
        except FileNotFoundError as exc:
            raise CompressAudioError(f"ffmpeg not found at {self._ffmpeg}") from exc
        except subprocess.TimeoutExpired as exc:
            raise CompressAudioError(f"ffmpeg timed out for {source_path}") from exc
        if result.returncode != 0:
            tmp_target.unlink(missing_ok=True)
            detail = result.stderr.strip() or "ffmpeg failed"
            raise CompressAudioError(f"ffmpeg error: {detail}")
        if not tmp_target.exists():
            raise CompressAudioError("ffmpeg did not produce the expected M4A file")

        os.replace(tmp_target, target)
        logger.info("audio_compressed", source=str(source), target=str(target))
        return str(target)
