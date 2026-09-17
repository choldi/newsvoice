"""Text-to-Speech service using the OmniVoice API (stock voice synthesis).

The digest markdown is converted to speech-friendly plain text, fragmented into
small paragraphs (<= tts_chunk_max_chars) to avoid long-input hallucinations in
the TTS model, synthesized chunk by chunk via POST /tts, and the resulting WAV
segments are concatenated into a single audio file.
"""

import asyncio
import hashlib
import io
import re
import wave
from pathlib import Path

import httpx
import structlog

from src.api.models.tts import TTSIn, TTSOut
from src.api.services.voices import VoicesService
from src.core.config import get_settings

logger = structlog.get_logger(__name__)


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
        stripped = re.sub(r"^#{1,6}\s+", "", stripped)
        stripped = re.sub(r"^\s*[-*+]\s+", "", stripped)
        stripped = re.sub(r"^\s*\d+\.\s*", "", stripped)
        lines.append(stripped)
    return "\n".join(line for line in lines if line)


def _split_sentences(paragraph: str) -> list[str]:
    """Split a paragraph into sentences, keeping punctuation with each one.

    Args:
        paragraph: Text to split.

    Returns:
        List of sentence strings.
    """
    sentences = re.split(r"(?<=[.!?])\s+", paragraph.strip())
    result: list[str] = []
    for sentence in sentences:
        # Avoid emitting a trailing empty chunk.
        if sentence:
            result.append(sentence)
    return result


def _fragment_text(text: str, max_chars: int = 400) -> list[str]:
    """Split text into small paragraph-sized fragments for TTS synthesis.

    Paragraphs are kept intact when short enough; longer paragraphs are split at
    sentence boundaries. Emotion tags stay at the start of their own fragment.

    Args:
        text: Plain text (emotion tags allowed).
        max_chars: Maximum length of each fragment.

    Returns:
        List of text fragments.
    """
    paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    fragments: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            fragments.append(paragraph)
            continue
        current = ""
        for sentence in _split_sentences(paragraph):
            candidate = f"{current} {sentence}".strip() if current else sentence
            if len(candidate) <= max_chars:
                current = candidate
            else:
                if current:
                    fragments.append(current)
                current = sentence
        if current:
            fragments.append(current)
    return fragments


def _concat_wav(chunks: list[bytes]) -> bytes:
    """Concatenate WAV payloads into a single WAV file.

    Args:
        chunks: List of WAV file bytes.

    Returns:
        A single merged WAV file's bytes.

    Raises:
        ValueError: If chunks is empty.
    """
    if not chunks:
        raise ValueError("No audio chunks to concatenate")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        for index, chunk in enumerate(chunks):
            with wave.open(io.BytesIO(chunk), "rb") as reader:
                if index == 0:
                    writer.setnchannels(reader.getnchannels())
                    writer.setsampwidth(reader.getsampwidth())
                    writer.setframerate(reader.getframerate())
                writer.writeframes(reader.readframes(reader.getnframes()))
    return buffer.getvalue()


class TTSService:
    """Service to convert digest markdown into WAV audio via the OmniVoice API."""

    def __init__(self, cache_dir: str | None = None) -> None:
        """Initializes the TTSService with an optional cache directory.

        Args:
            cache_dir: Optional path to the TTS cache directory. Defaults to settings.tts_cache_dir.
        """
        settings = get_settings()
        self._cache_dir = Path(cache_dir or settings.tts_cache_dir)
        self._api_base_url = settings.tts_api_base_url
        self._api_key = settings.tts_api_key
        self._default_voice = settings.tts_voice
        self._default_language = settings.tts_language
        self._max_chars = settings.tts_max_chars
        self._chunk_max_chars = settings.tts_chunk_max_chars

    async def _resolve_voice_id(self, voice: str) -> str:
        """Resolve a voice name or stock ID to the OmniVoice voice_id.

        Stock voices are returned unchanged.  For cloned voices the friendly
        name stored in ``collections.toml`` is looked up via
        ``GET /voices/cloned`` and resolved to the UUID ``id`` that OmniVoice
        expects in ``POST /tts``.

        Args:
            voice: Configured voice name or stock voice id.

        Returns:
            The ``voice_id`` to send to OmniVoice.
        """
        voices_svc = VoicesService(self._api_base_url, self._api_key)
        stock = await voices_svc.list_stock(force=True)
        stock_ids = {v.get("voice_id") for v in stock}
        if voice in stock_ids:
            return voice
        cloned = await voices_svc.list_cloned(force=True)
        for entry in cloned:
            if entry.get("name") == voice:
                clone_id = entry.get("id")
                if clone_id:
                    return clone_id
        return voice

    async def generate_tts(self, payload: TTSIn) -> TTSOut:
        """Generate (or fetch from cache) a WAV for the given markdown.

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
        audio_path = self._cache_dir / f"{content_hash}.wav"

        self._cache_dir.mkdir(parents=True, exist_ok=True)

        cached = audio_path.exists()
        if not cached:
            resolved_voice = await self._resolve_voice_id(voice)
            fragments = _fragment_text(speech_text, self._chunk_max_chars)
            chunk_wavs = await self._synthesize_fragments(fragments, resolved_voice, language)
            audio_bytes = _concat_wav(chunk_wavs)
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

    async def _synthesize_fragments(
        self, fragments: list[str], voice: str, language: str
    ) -> list[bytes]:
        """Synthesize fragments concurrently, preserving input order.

        Long digests fragment into many small pieces; synthesizing them one at
        a time can exceed the workflow's TTS activity timeout. A bounded number
        of concurrent requests keeps wall time low while avoiding overloading
        the (single-GPU) OmniVoice backend.

        Args:
            fragments: Text fragments to synthesize.
            voice: Resolved OmniVoice voice id.
            language: ISO 639-1 language code.

        Returns:
            List of WAV bytes, ordered like ``fragments``.
        """
        settings = get_settings()
        semaphore = asyncio.Semaphore(settings.tts_synthesis_concurrency)

        async def synthesize(fragment: str) -> bytes:
            async with semaphore:
                return await self._synthesize(fragment, voice, language)

        return list(await asyncio.gather(*(synthesize(f) for f in fragments)))

    async def _synthesize(self, text: str, voice: str, language: str) -> bytes:
        """Call OmniVoice POST /tts and return the raw WAV bytes.

        Args:
            text: Text fragment to synthesize.
            voice: OmniVoice stock voice identifier.
            language: ISO 639-1 language code.

        Returns:
            Audio/wav response bytes.

        Raises:
            httpx.HTTPStatusError: If the API returns a non-2xx status.
        """
        settings = get_settings()
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        payload = {"text": text, "voice_id": voice, "language": language}
        url = self._api_base_url.rstrip("/") + "/tts"
        async with httpx.AsyncClient(timeout=settings.tts_request_timeout) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response.content


async def list_cached_audios(cache_dir: str | None = None) -> list[str]:
    """Return the list of cached audio filenames (utility for ops/debug).

    Args:
        cache_dir: Optional cache directory path. Defaults to settings.tts_cache_dir.

    Returns:
        Sorted list of cached WAV filenames.
    """
    settings = get_settings()
    base = Path(cache_dir or settings.tts_cache_dir)
    if not base.exists():
        return []
    return sorted(p.name for p in base.glob("*.wav"))
