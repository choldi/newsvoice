"""Text-to-Speech service for the digest audio (Phase 8: multi-engine).

The digest markdown is converted to speech-friendly plain text and synthesized
by the engine the collection asks for (``tts_backend``): client-side
``edge-tts`` (the default), the ``tts-api`` gateway, or a Modal backend. The
dispatch itself lives in :mod:`src.api.services.tts_backends`; this module owns
everything around it — truncation, markdown stripping, the content-addressed
cache and the per-backend output extension.

Edge neural voices read the text verbatim, eliminating the hallucination /
repetition artifacts seen with the previous LLM-based OmniVoice backend; the
WAV/MP3 is later transcoded to M4A (AAC) before delivery (see
``AudioCompressService``), which is why the stored extension only has to be
something ffmpeg can read.
"""

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import edge_tts
import structlog

from src.api.models.narration import TTSBackend
from src.api.models.tts import TTSIn, TTSOut
from src.api.services.audio_files import atomic_write, read_sidecar, sidecar_path
from src.api.services.tts_backends import (
    ext_for,
    resolve_backend,
    voice_for,
)
from src.api.services.tts_backends import (
    synthesize as synthesize_with_backend,
)
from src.api.services.tts_estimate import estimate_synthesis
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


async def _available_voice_names() -> set[str]:
    """Return the set of Edge voice ShortNames (cached at module level).

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


# Schema version stored in the cache sidecar, so a future change of the hashing
# inputs invalidates every cached file instead of reusing a wrong audio.
# v2: the backend is part of the hash, so flipping a collection from edge to the
# gateway regenerates the audio instead of serving Edge bytes under a gateway
# voice. The hash itself changed too, which is what actually invalidates old
# sidecars (the version is recorded, not compared).
SIDECAR_VERSION = 2


def _content_hash(
    speech_text: str, voice: str, language: str, backend: TTSBackend = TTSBackend.EDGE
) -> str:
    """Hash the inputs that fully determine a synthesis.

    Args:
        speech_text: Speech-friendly text handed to the TTS engine.
        voice: Resolved voice id, in the backend's own vocabulary.
        language: ISO 639-1 language code.
        backend: Engine that will synthesize (different engines, different audio).

    Returns:
        Hex SHA-256 digest.
    """
    return hashlib.sha256(
        "\x1f".join((str(backend), voice, language, speech_text)).encode()
    ).hexdigest()


def _audio_filename(collection: str, day: datetime | None = None, ext: str = ".mp3") -> str:
    """Build the delivery filename news_<collection>_<yymmdd><ext>.

    The collection is sanitized to [a-z0-9_-] (falling back to 'default') so
    arbitrary collection names never leak into filesystem paths. The date is
    the UTC day of the run, formatted as yymmdd. The stem is the Phase 5.2
    contract; only the extension follows the engine (``.mp3`` for edge,
    ``.wav`` for the gateway and the Modal backends).

    Args:
        collection: Collection name owning this digest audio.
        day: Optional datetime driving the date stamp. Defaults to now (UTC).
        ext: Extension including the dot, from :func:`tts_backends.ext_for`.

    Returns:
        Filename like 'news_health_260921.mp3'.
    """
    name = re.sub(r"[^a-z0-9_-]+", "_", collection.strip().lower()).strip("_-") or "default"
    stamp = (day or datetime.now(UTC)).strftime("%y%m%d")
    return f"news_{name}_{stamp}{ext}"


def estimate_duration_seconds(char_count: int) -> float:
    """Rough duration estimate in seconds (~15 chars/sec for Spanish TTS).

    Args:
        char_count: Number of characters handed to the engine.

    Returns:
        Estimated duration in seconds.
    """
    return round(char_count / 15.0, 2)


def _estimate_duration(char_count: int) -> float:
    """Rough duration estimate in seconds (delegate to the shared helper)."""
    return estimate_duration_seconds(char_count)


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


def speech_text_for_digest(markdown: str, max_chars: int | None = None) -> str:
    """Return the exact plain text ``generate_tts`` hands to the engine.

    Phase 7.14 verifies audio against what was *actually* synthesized, so the
    truncation and the markdown stripping must be the same code path, not a
    copy of it.

    Args:
        markdown: Digest markdown.
        max_chars: Truncation limit; defaults to ``settings.tts_max_chars``.

    Returns:
        Speech-ready plain text.
    """
    limit = settings_tts_max_chars() if max_chars is None else max_chars
    return _markdown_to_speech_text(_truncate_for_tts(markdown, limit))


def settings_tts_max_chars() -> int:
    """Return the configured digest truncation limit.

    Returns:
        ``settings.tts_max_chars``.
    """
    return get_settings().tts_max_chars


def markdown_to_speech_text(text: str) -> str:
    """Public entry point for markdown-to-speech conversion (Phase 7.11).

    Other services (document extraction) must strip markdown exactly the way
    the digest TTS does, so they call this instead of re-implementing the
    rules.

    Args:
        text: Markdown (or plain) text to convert.

    Returns:
        Plain, speech-friendly text.
    """
    return _markdown_to_speech_text(text)


async def resolve_edge_voice(voice: str, language: str) -> str:
    """Map a configured voice name to a valid Edge neural ShortName.

    Tries an exact Edge catalogue match first, then legacy OmniVoice aliases,
    and finally a language-based default. The lookup does not require the
    network when an alias or the language default applies.

    Args:
        voice: Configured voice name (Edge ShortName, legacy alias, or free text).
        language: ISO 639-1 language code driving the fallback default.

    Returns:
        A valid Edge voice ShortName.
    """
    requested = voice.strip().lower()
    if not requested:
        return DEFAULT_VOICE_BY_LANGUAGE.get(language, "es-MX-JorgeNeural")

    known = await _available_voice_names()
    if requested in known:
        return voice.strip()

    alias = LEGACY_VOICE_ALIASES.get(requested)
    if alias:
        return alias

    logger.info("tts_voice_not_matched_falling_back", voice=voice, language=language)
    return DEFAULT_VOICE_BY_LANGUAGE.get(language, "es-MX-JorgeNeural")


async def synthesize_mp3(text: str, voice: str, *, timeout: int | None = None) -> bytes:
    """Synthesize text with an Edge neural voice, returning the MP3 bytes.

    Args:
        text: Speech-friendly plain text to synthesize.
        voice: Valid Edge voice ShortName.
        timeout: Seconds before the call is abandoned. Defaults to
            ``settings.tts_request_timeout``.

    Returns:
        Raw MP3 audio bytes.

    Raises:
        RuntimeError: If synthesis yields no audio or times out.
    """
    if not text.strip():
        raise RuntimeError("No text to synthesize")
    limit = timeout if timeout is not None else get_settings().tts_request_timeout
    communicate = edge_tts.Communicate(text, voice)
    chunks: list[bytes] = []

    async def _collect() -> None:
        async for item in communicate.stream():
            if item["type"] == "audio":
                chunks.append(item["data"])

    try:
        await asyncio.wait_for(_collect(), timeout=limit)
    except TimeoutError as exc:
        raise RuntimeError(f"Edge TTS synthesis timed out after {limit}s") from exc
    if not chunks:
        raise RuntimeError(f"Edge TTS produced no audio for voice {voice}")
    return b"".join(chunks)


@dataclass(frozen=True)
class _Prepared:
    """Everything a synthesis or a cache probe needs, resolved once."""

    speech_text: str
    """Plain text handed to the engine (truncated and markdown-stripped)."""
    backend: TTSBackend
    """Engine that will synthesize (or did, for a cache hit)."""
    voice: str
    """Voice id in the engine's own vocabulary."""
    language: str
    """ISO 639-1 language code."""
    audio_path: Path
    """Destination file, with the backend's extension."""
    digest: str
    """Content hash stored in the sidecar."""


class TTSService:
    """Convert digest markdown into a cached audio file with the chosen engine."""

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
        return await _available_voice_names()

    async def _resolve_voice(self, voice: str, language: str) -> str:
        """Map a configured voice name to a valid Edge voice ShortName.

        Args:
            voice: Configured voice name (Edge ShortName, legacy alias, or free text).
            language: ISO 639-1 language code driving the fallback default.

        Returns:
            A valid Edge voice ShortName.
        """
        return await resolve_edge_voice(voice, language)

    async def _prepare(self, payload: TTSIn) -> _Prepared:
        """Resolve the engine, the voice and the cache key for one request.

        Args:
            payload: TTSIn model containing the markdown text and options.

        Returns:
            The resolved inputs, ready for a cache check or a synthesis.

        Raises:
            TTSBackendError: When the backend is unknown or has no usable voice
                for this configuration.
        """
        language = payload.language or self._default_language
        backend = resolve_backend(payload.backend)
        resolved_voice = await voice_for(backend, payload.voice or self._default_voice, language)
        speech_text = speech_text_for_digest(payload.markdown, self._max_chars)
        audio_path = self._cache_dir / _audio_filename(payload.collection, ext=ext_for(backend))
        return _Prepared(
            speech_text=speech_text,
            backend=backend,
            voice=resolved_voice,
            language=language,
            audio_path=audio_path,
            digest=_content_hash(speech_text, resolved_voice, language, backend),
        )

    async def cache_probe(self, payload: TTSIn) -> bool:
        """Report whether this exact synthesis is already on disk.

        A cache hit is free, so callers that charge for synthesis (the Modal
        budget guard) must consult this first: an exhausted budget must never
        block audio that already exists.

        Args:
            payload: TTSIn model containing the markdown text and options.

        Returns:
            True when a matching audio file and sidecar are present.
        """
        prepared = await self._prepare(payload)
        if not prepared.audio_path.exists():
            return False
        sidecar = read_sidecar(sidecar_path(prepared.audio_path)) or {}
        return sidecar.get("sha256") == prepared.digest

    async def generate_tts(self, payload: TTSIn) -> TTSOut:
        """Generate (or fetch from cache) the audio for the given markdown.

        The cache is keyed by everything that determines the audio: the speech
        text, the resolved voice, the language **and the engine**. The digest is
        stored in a JSON sidecar next to the audio file, so editing the digest,
        changing the voice, switching the engine or upgrading the cache schema
        always regenerates the audio instead of serving bytes another engine
        produced.

        Args:
            payload: TTSIn model containing the markdown text and optional
                voice/language/backend.

        Returns:
            TTSOut with the audio path, the duration estimate and cache status.

        Raises:
            TTSBackendError: When the configured engine cannot serve this
                request (unknown backend, voice outside its catalogue).
        """
        prepared = await self._prepare(payload)
        sidecar = sidecar_path(prepared.audio_path)

        self._cache_dir.mkdir(parents=True, exist_ok=True)

        existed = prepared.audio_path.exists()
        cached = existed and (read_sidecar(sidecar) or {}).get("sha256") == prepared.digest
        used_fallback = False

        if not cached:
            audio_bytes, used_fallback = await self._synthesize(prepared)
            atomic_write(prepared.audio_path, audio_bytes)
            if used_fallback:
                # The bytes on disk came from another engine than the one the
                # cache key names, so the sidecar is deliberately left out: the
                # next run must retry the requested engine instead of pinning
                # this degraded audio as its permanent answer.
                logger.warning(
                    "tts_fallback_used",
                    requested_backend=str(prepared.backend),
                    audio_path=str(prepared.audio_path),
                )
            else:
                atomic_write(
                    sidecar,
                    json.dumps(
                        {
                            "version": SIDECAR_VERSION,
                            "sha256": prepared.digest,
                            "backend": str(prepared.backend),
                            "voice": prepared.voice,
                            "language": prepared.language,
                            "char_count": len(prepared.speech_text),
                            "generated_at": datetime.now(UTC).isoformat(),
                        },
                        indent=2,
                        sort_keys=True,
                    ).encode(),
                )
            logger.info(
                "tts_cache_regenerated",
                reason="content_changed" if existed else "missing",
                backend=str(prepared.backend),
                voice=prepared.voice,
                audio_path=str(prepared.audio_path),
            )

        logger.info(
            "tts_generate_done",
            backend=str(prepared.backend),
            voice=payload.voice,
            language=prepared.language,
            char_count=len(prepared.speech_text),
            cached=cached,
            audio_path=str(prepared.audio_path),
        )

        # Phase 8.5: how long this *would* have taken. A cache hit costs zero,
        # and a fallback run already knows it spent edge-time, not engine-time.
        wall_seconds = (
            0.0
            if cached
            else estimate_synthesis(
                [prepared.speech_text], backend=prepared.backend, concurrency=1
            ).wall_seconds
        )

        return TTSOut(
            audio_path=str(prepared.audio_path),
            duration_estimate=_estimate_duration(len(prepared.speech_text)),
            estimate_wall_seconds=wall_seconds,
            cached=cached,
            voice=payload.voice or self._default_voice,
            backend=str(TTSBackend.EDGE if used_fallback else prepared.backend),
            char_count=len(prepared.speech_text),
        )

    async def _synthesize(self, prepared: _Prepared) -> tuple[bytes, bool]:
        """Synthesize the prepared text with its engine, optionally degrading to edge.

        A gateway or Modal failure must not cost the digest its audio when the
        operator opts in (``TTS_FALLBACK_TO_EDGE``). The default is off, on
        purpose: silently swapping a clone or a designed voice for a stock Edge
        voice is what ``NARRATIONS_DIALOG_FALLBACK_TO_EDGE=false`` exists to
        prevent — the voice is part of the product, not an implementation detail.

        Args:
            prepared: Resolved inputs from :meth:`_prepare`.

        Returns:
            Tuple of ``(audio_bytes, used_fallback)``. ``used_fallback`` is True
            when edge produced the bytes instead of the requested engine.

        Raises:
            RuntimeError: If synthesis yields no audio or times out.
            TTSBackendError: If the engine is unavailable or rejects the call and
                the fallback is disabled or itself fails.
        """
        try:
            audio = await synthesize_with_backend(
                prepared.backend,
                prepared.speech_text,
                prepared.voice,
                prepared.language,
                timeout=float(self._request_timeout),
            )
            return audio, False
        except Exception as exc:
            if prepared.backend is TTSBackend.EDGE or not get_settings().tts_fallback_to_edge:
                raise
            logger.warning(
                "tts_backend_failed_falling_back_to_edge",
                backend=str(prepared.backend),
                voice=prepared.voice,
                error=str(exc),
            )
            fallback_voice = await self._resolve_voice("", prepared.language)
            return (
                await synthesize_mp3(
                    prepared.speech_text, fallback_voice, timeout=self._request_timeout
                ),
                True,
            )


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
