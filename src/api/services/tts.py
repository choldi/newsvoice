"""Text-to-Speech service using edge-tts."""

import hashlib
from pathlib import Path

import edge_tts
import structlog

from src.api.models.tts import TTSIn, TTSOut
from src.core.config import get_settings

logger = structlog.get_logger(__name__)


def _content_hash(markdown: str, voice: str) -> str:
    """Stable SHA256 hash of the (markdown, voice) pair."""
    payload = f"{voice}::{markdown}".encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _estimate_duration(char_count: int) -> float:
    """Rough duration estimate in seconds (~15 chars/sec for Spanish TTS)."""
    return round(char_count / 15.0, 2)


def _truncate_for_tts(text: str, max_chars: int) -> str:
    """Truncate text to max_chars, breaking at the nearest paragraph boundary."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_break = max(truncated.rfind("\n\n"), truncated.rfind(". "))
    if last_break > max_chars // 2:
        truncated = truncated[: last_break + 1]
    return truncated.strip()


class TTSService:
    """Service to convert digest markdown into MP3 audio using edge-tts."""

    def __init__(self, cache_dir: str | None = None) -> None:
        """Initializes the TTSService with an optional cache directory.

        Args:
            cache_dir: Optional path to the TTS cache directory. Defaults to settings.tts_cache_dir.
        """
        settings = get_settings()
        self._cache_dir = Path(cache_dir or settings.tts_cache_dir)
        self._default_voice = settings.tts_voice
        self._max_chars = settings.tts_max_chars

    async def generate_tts(self, payload: TTSIn) -> TTSOut:
        """Generate (or fetch from cache) an MP3 for the given markdown.

        Args:
            payload: TTSIn model containing the markdown text and optional voice.

        Returns:
            TTSOut model with the audio file path, duration estimate, and cache status.
        """
        voice = payload.voice or self._default_voice
        text = _truncate_for_tts(payload.markdown, self._max_chars)
        content_hash = _content_hash(text, voice)
        audio_path = self._cache_dir / f"{content_hash}.mp3"

        self._cache_dir.mkdir(parents=True, exist_ok=True)

        cached = audio_path.exists()
        if not cached:
            await self._synthesize(text, voice, audio_path)

        duration = _estimate_duration(len(text))

        logger.info(
            "tts_generate_done",
            voice=voice,
            char_count=len(text),
            cached=cached,
            audio_path=str(audio_path),
        )

        return TTSOut(
            audio_path=str(audio_path),
            duration_estimate=duration,
            cached=cached,
            voice=voice,
            char_count=len(text),
        )

    @staticmethod
    async def _synthesize(text: str, voice: str, output_path: Path) -> None:
        """Run edge-tts and write the MP3 to output_path.

        Args:
            text: Text to synthesize.
            voice: TTS voice identifier (e.g., 'es-ES-ElviraNeural').
            output_path: Destination path for the MP3 file.
        """
        communicate = edge_tts.Communicate(text=text, voice=voice)
        await communicate.save(str(output_path))


async def list_cached_audios(cache_dir: str | None = None) -> list[str]:
    """Return the list of cached audio filenames (utility for ops/debug).

    Args:
        cache_dir: Optional cache directory path. Defaults to settings.tts_cache_dir.

    Returns:
        Sorted list of cached MP3 filenames.
    """
    settings = get_settings()
    base = Path(cache_dir or settings.tts_cache_dir)
    if not base.exists():
        return []
    return sorted(p.name for p in base.glob("*.mp3"))
