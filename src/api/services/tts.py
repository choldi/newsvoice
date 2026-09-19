"""Text-to-Speech service using Microsoft Edge neural voices (edge-tts).

The digest markdown is converted to speech-friendly plain text and synthesized
with ``edge_tts`` into a single MP3 file. Edge neural voices read the text
verbatim, eliminating the hallucination/repetition artifacts seen with the
previous LLM-based OmniVoice backend. The MP3 is later transcoded to M4A (AAC)
before delivery (see ``AudioCompressService``).
"""

import asyncio
import hashlib
import re
from pathlib import Path

import edge_tts
import structlog

from src.api.models.tts import TTSIn, TTSOut
from src.core.config import get_settings

logger = structlog.get_logger(__name__)

# Legacy OmniVoice voice names are transparently mapped to Edge neural voices so
# existing collections.toml files keep working after the migration.
LEGACY_VOICE_ALIASES = {
    "es-mx-male": "es-MX-JorgeNeural",
    "es-mx-female": "es-MX-DaliaNeural",
    "es-mx": "es-MX-JorgeNeural",
    "en-us-female": "en-US-AriaNeural",
    "en-us-male": "en-US-GuyNeural",
    "en-gb-female": "en-GB-SoniaNeural",
    "en-gb-male": "en-GB-RyanNeural",
}

# Fallback neural voice per ISO 639-1 language when the configured voice cannot
# be matched against the Edge catalogue or the network is unavailable.
DEFAULT_VOICE_BY_LANGUAGE = {
    "es": "es-MX-JorgeNeural",
    "en": "en-US-GuyNeural",
    "pt": "pt-BR-AntonioNeural",
    "it": "it-IT-DiegoNeural",
    "fr": "fr-FR-HenriNeural",
    "de": "de-DE-ConradNeural",
}

# Lazily-loaded catalogue of available Edge voices ({ShortName}). Kept at module
# level so a workflow run (many activity calls) only fetches it once.
_EDGE_VOICES: list[dict] | None = None


def _content_hash(markdown: str, voice: str, language: str = "") -> str:
    """Stable SHA256 hash of the (markdown, voice, language) triple."""
    payload = f"{voice}::{language}::{markdown}".encode()
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


def _markdown_to_speech_text(text: str) -> str:
    """Strip markdown syntax, keeping inline OmniVoice emotion tags untouched.

    Args:
        text: Digest markdown that may contain emotion tags like '[happy]'.

    Returns:
        Plain, speech-friendly text.
    """
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        stripped = re.sub(r"^#+(?:\s.*)?$", lambda m: m.group(0).lstrip("#").strip(), stripped)
        stripped = re.sub(r"^\s*[-*+]\s+", "", stripped)
        stripped = re.sub(r"^\s*\d+\.\s*", "", stripped)
        lines.append(stripped)
    return "\n".join(line for line in lines if line)


class TTSService:
    """Service to convert digest markdown into MP3 audio via Edge neural TTS."""

    def __init__(self, cache_dir: str | None = None) -> None:
        """Initialize the TTSService with an optional cache directory.

        Args:
            cache_dir: Optional path to the TTS cache directory. Defaults to settings.tts_cache_dir.
        """
        settings = get_settings()
        self._cache_dir = Path(cache_dir or settings.tts_cache_dir)
        self._default_voice = settings.tts_voice or DEFAULT_VOICE_BY_LANGUAGE.get(
            settings.tts_language, "es-MX-JorgeNeural"
        )
        self._default_language = settings.tts_language
        self._max_chars = settings.tts_max_chars
        self._request_timeout = settings.tts_request_timeout

    async def _available_voice_names(self) -> set[str]:
        """Return the set of Edge voice ShortNames (cached across instances).

        Returns:
            Lowercased ShortName set. Empty when the catalogue cannot be fetched.
        """
        global _EDGE_VOICES
        if _EDGE_VOICES is None:
            try:
                _EDGE_VOICES = await edge_tts.list_voices()
            except Exception as exc:
                logger.warning("edge_voices_unavailable", error=str(exc))
                _EDGE_VOICES = []
        return {v.get("ShortName", "").lower() for v in _EDGE_VOICES}

    async def _resolve_voice(self, voice: str, language: str) -> str:
        """Map a configured voice name to a valid Edge neural ShortName.

        Tries an exact Edge catalogue match first, then legacy OmniVoice
        aliases, and finally a language-based default. The lookup does not
        require the network when an alias or the language default applies.

        Args:
            voice: Configured voice name (Edge ShortName, legacy alias, or free text).
            language: ISO 639-1 language code driving the fallback default.

        Returns:
            A valid Edge voice ShortName.
        """
        requested = voice.strip().lower()
        if not requested:
            return DEFAULT_VOICE_BY_LANGUAGE.get(language, "es-MX-JorgeNeural")

        known = await self._available_voice_names()
        if requested in known:
            return voice.strip()

        alias = LEGACY_VOICE_ALIASES.get(requested)
        if alias:
            return alias

        logger.info("tts_voice_not_matched_falling_back", voice=voice, language=language)
        return DEFAULT_VOICE_BY_LANGUAGE.get(language, "es-MX-JorgeNeural")

    async def generate_tts(self, payload: TTSIn) -> TTSOut:
        """Generate (or fetch from cache) an MP3 for the given markdown.

        Args:
            payload: TTSIn model containing the markdown text and optional voice/language.

        Returns:
            TTSOut model with the audio file path, duration estimate, and cache status.
        """
        voice = payload.voice or self._default_voice
        language = payload.language or self._default_language
        text = _truncate_for_tts(payload.markdown, self._max_chars)
        speech_text = _markdown_to_speech_text(text)
        content_hash = _content_hash(text, voice, language)
        audio_path = self._cache_dir / f"{content_hash}.mp3"

        self._cache_dir.mkdir(parents=True, exist_ok=True)

        cached = audio_path.exists()
        if not cached:
            resolved_voice = await self._resolve_voice(voice, language)
            audio_bytes = await self._synthesize(speech_text, resolved_voice)
            audio_path.write_bytes(audio_bytes)

        duration = _estimate_duration(len(speech_text))

        logger.info(
            "tts_generate_done",
            voice=voice,
            language=language,
            char_count=len(speech_text),
            cached=cached,
            audio_path=str(audio_path),
        )

        return TTSOut(
            audio_path=str(audio_path),
            duration_estimate=duration,
            cached=cached,
            voice=voice,
            char_count=len(speech_text),
        )

    async def _synthesize(self, text: str, voice: str) -> bytes:
        """Synthesize text with the Edge neural voice, returning the MP3 bytes.

        Args:
            text: Speech-friendly plain text to synthesize.
            voice: Valid Edge voice ShortName.

        Returns:
            Raw MP3 audio bytes.

        Raises:
            RuntimeError: If synthesis yields no audio or times out.
        """
        if not text.strip():
            raise RuntimeError("No text to synthesize")
        communicate = edge_tts.Communicate(text, voice)
        chunks: list[bytes] = []

        async def _collect() -> None:
            async for item in communicate.stream():
                if item["type"] == "audio":
                    chunks.append(item["data"])

        try:
            await asyncio.wait_for(_collect(), timeout=self._request_timeout)
        except TimeoutError as exc:
            raise RuntimeError(
                f"Edge TTS synthesis timed out after {self._request_timeout}s"
            ) from exc
        if not chunks:
            raise RuntimeError(f"Edge TTS produced no audio for voice {voice}")

        return b"".join(chunks)


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
