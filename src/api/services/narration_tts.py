from __future__ import annotations
from src.core.tracing import trace_function

"""Narration rendering service: multi-speaker audio via the TTS gateway (Phase 7).

The digest TTS path (:mod:`src.api.services.tts`) speaks a whole document with a
single Edge voice. This service instead renders a *script*: an ordered list of
turns, each one spoken by a different role, optionally laid over a local music
bed and encoded to M4A for delivery.

Design notes, all of them forced by how the gateway actually behaves:

- **The gateway does not chunk.** Long inputs are split client-side at sentence
  (then clause, then word) boundaries, never mid-word.
- **The gateway caps concurrency.** All calls go through a semaphore sized by
  ``narration_max_concurrency``.
- **The gateway has no server-side timeout.** The client applies
  ``narration_request_timeout`` and falls back to a direct Edge call.
- **``/conversations`` splices raw frames using the *last* turn's sample
  rate.** Mixing engines with different rates in one request therefore produces
  mangled audio, so the single-request path is an explicit opt-in
  (:class:`RenderStrategy`) and the default is turn-by-turn synthesis with local
  resampling. The endpoint applies one global speed (it still ignores
  per-turn emotion) and needs at least two turns.

Narration audio is cached by content hash (spec + script) in
``narration_cache_dir``, next to a JSON sidecar, exactly like the digest TTS
cache, and lives outside ``tts_cache_dir`` so the two caches never collide.
"""


import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import edge_tts
import httpx
import structlog

from src.api.models.narration import (
    GatewayHealthOut,
    MusicBed,
    NarrationOut,
    NarrationScript,
    NarrationSpec,
    TTSBackend,
)
from src.api.services.audio_compress import AudioCompressService
from src.api.services.audio_files import atomic_write, read_sidecar, sidecar_path
from src.api.services.gateway_voices import resolve_gateway_qwen_voice
from src.api.services.tts import DEFAULT_VOICE_BY_LANGUAGE, LEGACY_VOICE_ALIASES
from src.core.audio import (
    CANONICAL_CHANNELS,
    CANONICAL_FORMAT,
    CANONICAL_RATE,
    AudioError,
    wav_duration,
    wav_format,
    wav_peak_ratio,
)
from src.core.audio import (
    concat_wavs as _concat_wavs,
)
from src.core.config import get_settings
from src.core.prompts import strip_emotion_tags

logger = structlog.get_logger(__name__)

# The gateway rejects conversations with fewer turns than this.
MIN_CONVERSATION_TURNS = 2

# Edge neural voices all end with this suffix, which is how a cast is proven to
# be homogeneous (and therefore safe for the single-request path).
EDGE_NEURAL_SUFFIX = "Neural"

# Bumped when the hashing inputs or the render pipeline change, so every cached
# narration is regenerated instead of a wrong audio being served.
SIDECAR_VERSION = 1

# Peak below this fraction of full scale (-60 dBFS) is inaudible. A render
# that ends up here has produced an empty podcast, which must fail loudly
# instead of being compressed, delivered and remembered as a cache hit.
SILENCE_PEAK_RATIO = 0.001

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_CLAUSE_SPLIT = re.compile(r"(?<=[,;:])\s+")


class NarrationError(RuntimeError):
    """Base error for the narration pipeline."""


class NarrationGatewayError(NarrationError):
    """Raised when the TTS gateway rejects a request or cannot be reached.

    Attributes:
        status_code: HTTP status returned by the gateway, or 0 for transport errors.
        error_type: Machine-readable type from the gateway error body, e.g.
            'unsupported_language' or 'voice_not_found'.
    """

    def __init__(self, message: str, status_code: int = 0, error_type: str = "") -> None:
        """Initialize the error.

        Args:
            message: Human-readable description.
            status_code: HTTP status code, or 0 for transport failures.
            error_type: Machine-readable gateway error type.
        """
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type


class NarrationRenderError(NarrationError, AudioError):
    """Raised when audio cannot be assembled, mixed or written.

    Subclasses :class:`~src.core.audio.AudioError` so callers that only care
    about "the audio payload was bad" can catch the base type, which is what
    the voice-design worker does.
    """


class RenderStrategy(StrEnum):
    """How a script is turned into audio.

    Attributes:
        TURNS: One gateway request per turn, assembled locally. Honors per-turn
            speed, emotion and pauses, and resamples engines that disagree on
            the sample rate. Default.
        CONVERSATIONS: A single ``/conversations`` request. Cheaper in round
            trips, but the gateway applies one speaking rate, no per-turn
            emotion, and splices every turn's frames at the last turn's sample
            rate, so it is only safe for a single-engine cast.
    """

    TURNS = "turns"
    CONVERSATIONS = "conversations"


def is_audible(data: bytes) -> bool:
    """Report whether a WAV payload carries audible content.

    Args:
        data: Raw WAV bytes.

    Returns:
        ``False`` only when the payload is measurable and its peak stays
        below :data:`SILENCE_PEAK_RATIO`.
    """
    peak = wav_peak_ratio(data)
    return peak is None or peak >= SILENCE_PEAK_RATIO


def concat_wavs(parts: Sequence[tuple[bytes, int]]) -> bytes:
    """Concatenate WAV payloads inserting a pause after each one.

    All parts must already share the same format; the caller is responsible for
    resampling (see :meth:`NarrationTTSService._to_canonical`).

    Args:
        parts: Sequence of ``(wav_bytes, pause_after_ms)`` pairs. The pause of
            the last element is ignored.

    Returns:
        A single WAV payload in canonical format.

    Raises:
        NarrationRenderError: If the parts have mismatched formats or are empty.
    """
    try:
        return _concat_wavs(parts)
    except AudioError as exc:
        raise NarrationRenderError(str(exc)) from exc


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Split text into synthesis-sized pieces without breaking words.

    Sentences are packed greedily up to ``max_chars``; an oversized sentence is
    split on clause boundaries and, as a last resort, on word boundaries.

    Args:
        text: Text to split.
        max_chars: Maximum characters per piece.

    Returns:
        List of non-empty trimmed pieces, in order.

    Raises:
        NarrationRenderError: If the text is empty or ``max_chars`` is not positive.
    """
    if not text.strip():
        raise NarrationRenderError("cannot chunk empty text")
    if max_chars <= 0:
        raise NarrationRenderError("max_chars must be positive")

    pieces: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(text.strip()):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= max_chars:
            pieces.append(sentence)
            continue
        for clause in _CLAUSE_SPLIT.split(sentence):
            clause = clause.strip()
            if not clause:
                continue
            if len(clause) <= max_chars:
                pieces.append(clause)
                continue
            pieces.extend(_split_on_words(clause, max_chars))
    return pieces


def _split_on_words(text: str, max_chars: int) -> list[str]:
    """Split text on word boundaries, packing words up to ``max_chars``.

    A single word longer than ``max_chars`` (a URL, a hashtag) has no boundary to
    split on, and the gateway does not chunk, so it is cut as a last resort with
    a warning: a broken token still gets spoken, an oversized request would fail
    the whole turn.

    Args:
        text: Text to split.
        max_chars: Maximum characters per piece.

    Returns:
        List of non-empty pieces.
    """
    pieces: list[str] = []
    current: list[str] = []
    length = 0
    for word in text.split():
        if len(word) > max_chars:
            if current:
                pieces.append(" ".join(current))
                current, length = [], 0
            logger.warning("narration_token_exceeds_chunk", chars=len(word), max_chars=max_chars)
            pieces.extend(
                word[start : start + max_chars] for start in range(0, len(word), max_chars)
            )
            continue
        if current and length + 1 + len(word) > max_chars:
            pieces.append(" ".join(current))
            current, length = [], 0
        current.append(word)
        length += len(word) + (1 if length else 0)
    if current:
        pieces.append(" ".join(current))
    return pieces


def split_turns_into_blocks(
    turns: Sequence[dict[str, Any]],
    *,
    max_chars: int,
    max_turns: int,
    min_turns: int,
) -> list[list[dict[str, Any]]]:
    """Split a turn list into conversation blocks.

    Blocks are cut at turn boundaries only: a turn is never split, so a block
    boundary always falls between two complete sentences (turns come from
    :func:`chunk_text`, which packs whole sentences). A trailing block with
    fewer turns than ``min_turns`` is merged into the previous one, which may
    push that block over the caps: a slightly larger request beats a tiny one.

    Args:
        turns: Turn dicts with 'voice_id', 'text' and 'language'.
        max_chars: Maximum characters per block.
        max_turns: Maximum turns per block.
        min_turns: Trailing blocks smaller than this are merged backwards.

    Returns:
        The turns grouped into blocks, in order. Empty when ``turns`` is empty.

    Raises:
        NarrationRenderError: If any cap is not positive.
    """
    if max_chars <= 0 or max_turns <= 0 or min_turns <= 0:
        raise NarrationRenderError("block caps must be positive")
    if not turns:
        return []
    blocks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for turn in turns:
        text = str(turn.get("text") or "")
        if current and (current_chars + len(text) > max_chars or len(current) >= max_turns):
            blocks.append(current)
            current, current_chars = [], 0
        current.append(turn)
        current_chars += len(text)
    if current:
        blocks.append(current)
    if len(blocks) > 1 and len(blocks[-1]) < min_turns:
        blocks[-2].extend(blocks[-1])
        blocks.pop()
    return blocks


def content_hash(
    spec: NarrationSpec, script: NarrationScript, strategy: RenderStrategy = RenderStrategy.TURNS
) -> str:
    """Hash everything that fully determines a narration render.

    Args:
        spec: Narration spec (cast, delivery, music).
        script: Script to render.
        strategy: Render strategy; different strategies produce different audio.

    Returns:
        Hex SHA-256 digest.
    """
    payload = json.dumps(
        {
            "version": SIDECAR_VERSION,
            "strategy": str(strategy),
            "spec": spec.model_dump(mode="json"),
            "script": script.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def narration_filename(collection: str, day: datetime | None = None, key: str = "") -> str:
    """Build the narration cache filename ``narrate_<collection>_<yymmdd>_<hash8>.wav``.

    The collection is sanitized to [a-z0-9_-] (falling back to 'default') so
    arbitrary names never leak into paths. The 8-char hash suffix keeps two
    different renders of the same day side by side instead of overwriting each
    other, which is what makes the shared ``.meta.json`` sidecar unambiguous.

    Args:
        collection: Collection name owning the narration.
        day: Optional datetime driving the date stamp. Defaults to now (UTC).
        key: Content hash; its first 8 characters are appended.

    Returns:
        Filename like 'narrate_tech_260926_1a2b3c4d.wav'.
    """
    name = re.sub(r"[^a-z0-9_-]+", "_", collection.strip().lower()).strip("_-") or "default"
    stamp = (day or datetime.now(UTC)).strftime("%y%m%d")
    suffix = key[:8] or "nohash"
    return f"narrate_{name}_{stamp}_{suffix}.wav"


def edge_voice_for(voice_id: str, language: str) -> str:
    """Resolve a role voice to an Edge neural ShortName for the fallback path.

    Legacy OmniVoice names are mapped through the alias table, Edge names are
    used verbatim, and anything else falls back to the per-language default.

    Args:
        voice_id: Voice configured for the role (gateway id or Edge ShortName).
        language: ISO 639-1 language of the role.

    Returns:
        A valid Edge neural voice ShortName.
    """
    requested = voice_id.strip()
    alias = LEGACY_VOICE_ALIASES.get(requested.lower())
    if alias:
        return alias
    if requested.endswith(EDGE_NEURAL_SUFFIX):
        return requested
    return DEFAULT_VOICE_BY_LANGUAGE.get(language, "es-MX-JorgeNeural")


class NarrationTTSService:
    """Render a narration script to a cached, deliverable M4A file."""

    def __init__(
        self,
        cache_dir: str | None = None,
        music_dir: str | None = None,
        api_base_url: str | None = None,
        api_key: str | None = None,
        ffmpeg_path: str | None = None,
        bitrate_kbps: int | None = None,
        compressor: AudioCompressService | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialize the service with optional overrides.

        Args:
            cache_dir: Narration cache directory. Defaults to settings.narration_cache_dir.
            music_dir: Local music library. Defaults to settings.narration_music_dir.
            api_base_url: Gateway base URL. Defaults to settings.narration_base_url.
            api_key: Gateway API key. Defaults to settings.narration_api_key.
            ffmpeg_path: ffmpeg binary. Defaults to settings.ffmpeg_path.
            bitrate_kbps: M4A bitrate. Defaults to settings.narration_audio_bitrate_kbps.
            compressor: Injected M4A encoder (tests pass a stub).
            client: Injected HTTP client (tests pass a mock transport).
        """
        settings = get_settings()
        self._cache_dir = Path(cache_dir or settings.narration_cache_dir)
        self._music_dir = Path(music_dir or settings.narration_music_dir)
        self._api_base_url = (api_base_url or settings.narration_base_url).rstrip("/")
        self._api_key = settings.narration_api_key if api_key is None else api_key
        self._ffmpeg = ffmpeg_path or settings.ffmpeg_path
        self._compressor = compressor or AudioCompressService(
            ffmpeg_path=self._ffmpeg, bitrate_kbps=bitrate_kbps
        )
        self._client = client
        self._owns_client = client is None
        self._timeout = settings.narration_request_timeout
        self._max_chars = settings.narration_max_chars_per_request
        self._fallback_to_edge = settings.narration_fallback_to_edge
        self._dialog_fallback_to_edge = settings.narrations_dialog_fallback_to_edge
        self._semaphore = asyncio.Semaphore(settings.narration_max_concurrency)
        self._conv_max_chars = settings.narration_conversation_max_chars_per_block
        self._conv_max_turns = settings.narration_conversation_max_turns_per_block
        self._conv_min_turns = settings.narration_conversation_min_block_turns
        self._conv_retries = settings.narration_conversation_block_retries

    # --- Gateway plumbing ---------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """Build the gateway request headers.

        Returns:
            Header dict including X-API-Key when configured.
        """
        headers = {"Accept": "audio/wav, application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        return headers

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the HTTP client, creating it on first use.

        Returns:
            An httpx.AsyncClient bound to the configured base URL.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._api_base_url,
                timeout=self._timeout,
                headers=self._headers(),
            )
        return self._client

    async def aclose(self) -> None:
        """Close the HTTP client when the service owns it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _error_from_response(response: httpx.Response) -> NarrationGatewayError:
        """Translate a gateway error response into a typed error.

        The gateway answers with a nested body
        (``{"detail": {"detail": ..., "error_type": ...}}``), so both the nested
        and the flat shape are handled.

        Args:
            response: The non-2xx response.

        Returns:
            A NarrationGatewayError carrying status, type and message.
        """
        error_type = ""
        message = ""
        valid_voice_ids: list[str] = []
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            detail = body.get("detail")
            if isinstance(detail, dict):
                error_type = str(detail.get("error_type", ""))
                message = str(detail.get("detail", ""))
                valid = detail.get("valid_voice_ids")
                if isinstance(valid, list):
                    valid_voice_ids = [str(item) for item in valid]
            elif isinstance(detail, str):
                message = detail
            if not message:
                message = str(body.get("error", ""))
        if not message:
            message = f"gateway returned {response.status_code}"
        if valid_voice_ids:
            message = f"{message} (valid voice ids: {', '.join(valid_voice_ids[:8])})"
        return NarrationGatewayError(
            message, status_code=response.status_code, error_type=error_type
        )

    async def _post_audio(self, path: str, payload: dict[str, Any]) -> bytes:
        """POST a JSON payload to a gateway endpoint returning WAV bytes.

        Args:
            path: Endpoint path relative to the base URL (e.g. 'tts').
            payload: JSON body.

        Returns:
            The WAV payload returned by the gateway.

        Raises:
            NarrationGatewayError: On transport errors or non-2xx responses.
        """
        client = await self._get_client()
        async with self._semaphore:
            try:
                response = await client.post(path, json=payload)
            except httpx.HTTPError as exc:
                raise NarrationGatewayError(f"gateway request to {path} failed: {exc}") from exc
        if response.status_code >= 400:
            raise self._error_from_response(response)
        if not response.content:
            raise NarrationGatewayError(f"gateway returned an empty body for {path}")
        return response.content

    async def _post_modal_tts(
        self,
        text: str,
        voice_ref: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
        backend: TTSBackend = TTSBackend.QWEN3_TTS,
    ) -> bytes:
        """Synthesize one turn on Modal and return the WAV payload.

        The Modal client is imported lazily so the local path never needs the
        ``modal`` package or credentials.

        Args:
            text: Speech text for the turn.
            voice_ref: Reference voice name from the catalog.
            language: ISO 639-1 language code.
            speed: Speaking rate in [0.5, 2.0].
            emotion: Optional emotion tag, honored by ZONOS2 only.
            backend: Modal engine to call.

        Returns:
            The WAV payload returned by Modal.

        Raises:
            NarrationGatewayError: When Modal is disabled, unconfigured, or the
                call fails.
        """
        from src.core.modal_client import modal_tts, modal_zonos2_tts

        try:
            if backend is TTSBackend.ZONOS2:
                return await modal_zonos2_tts(
                    text=text,
                    voice_ref=voice_ref,
                    language=language,
                    speed=speed,
                    emotion=emotion,
                )
            return await modal_tts(
                text=text,
                voice_ref=voice_ref,
                language=language,
                speed=speed,
                emotion=emotion,
            )
        except Exception as exc:
            raise NarrationGatewayError(f"modal tts call failed: {exc}") from exc

    async def health(self) -> GatewayHealthOut:
        """Probe the gateway health endpoint.

        Never raises: an unreachable gateway is reported as
        ``reachable=False`` so callers can degrade instead of failing a run.

        Returns:
            The gateway health.
        """
        client = await self._get_client()
        try:
            async with self._semaphore:
                response = await client.get("health")
        except httpx.HTTPError as exc:
            logger.warning("narration_gateway_unreachable", error=str(exc))
            return GatewayHealthOut(reachable=False, status="unreachable", error=str(exc))
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if response.status_code >= 400 or not body:
            return GatewayHealthOut(
                reachable=False,
                status=str(body.get("status", "error")),
                engine=str(body.get("engine", "")),
                error=f"gateway health returned {response.status_code}",
            )
        health = GatewayHealthOut(
            reachable=True,
            status=str(body.get("status", "unknown")),
            engine=str(body.get("engine", "")),
            device=str(body.get("device", "")),
            mode=str(body.get("mode", "")),
            stock_voices_count=int(body.get("stock_voices_count", 0) or 0),
            gpu_available=bool(body.get("gpu_available", False)),
        )
        if health.status != "ok":
            health.error = f"gateway status is {health.status}"
        return health

    async def list_voices(self, language: str | None = None) -> list[dict[str, Any]]:
        """List the gateway voices, optionally filtered by language.

        Args:
            language: ISO 639-1 filter (e.g. 'es').

        Returns:
            List of voice dicts (empty when the gateway is unreachable).

        Raises:
            NarrationGatewayError: On non-2xx responses or transport errors.
        """
        client = await self._get_client()
        try:
            async with self._semaphore:
                response = await client.get("voices/stock")
        except httpx.HTTPError as exc:
            logger.warning("narration_voices_unreachable", error=str(exc))
            return []
        if response.status_code >= 400:
            raise self._error_from_response(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise NarrationGatewayError("gateway returned a non-JSON voice list") from exc
        if not isinstance(data, list):
            raise NarrationGatewayError("gateway returned an unexpected voice list")
        voices = [dict(item) for item in data if isinstance(item, dict)]
        if language:
            voices = [v for v in voices if v.get("language") == language]
        return voices

    # --- Synthesis ----------------------------------------------------------

    @trace_function(component="api.services.narration_tts")
    async def synthesize_turn(
        self,
        text: str,
        voice_id: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
        allow_edge_fallback: bool | None = None,
        backend: TTSBackend = TTSBackend.OMNIVOICE,
        run_in_modal: bool | None = None,
    ) -> tuple[bytes, bool]:
        """Synthesize one turn, dispatching on the configured backend.

        Args:
            text: Speech text for the turn.
            voice_id: Gateway voice id (stock name, clone UUID or Edge
                ShortName) for OmniVoice; reference voice name for the Modal
                backends.
            language: ISO 639-1 language of the text.
            speed: Speaking rate in [0.5, 2.0]. Honored by OmniVoice and, as a
                native ``rate`` hint, by Edge; the Modal backends post-process
                it (atempo / native control).
            emotion: Optional emotion tag (honored by OmniVoice only).
            allow_edge_fallback: Explicitly allow/disallow Edge fallback. ``None``
                (default) means use the instance default
                ``self._fallback_to_edge``.
            backend: Engine that synthesizes the turn.

        Returns:
            Tuple of ``(wav_bytes, used_edge_fallback)``. The fallback flag is
            always False for the direct backends (edge and Modal): only the
            OmniVoice gateway path can degrade to Edge.

        Raises:
            NarrationGatewayError: When the gateway fails and the fallback is
                disabled or itself fails, or when the Modal call fails.
        """
        if backend is TTSBackend.EDGE:
            return await self._edge_synthesize(
                text, edge_voice_for(voice_id, language), speed=speed
            ), False
        if backend is TTSBackend.QWEN3_TTS:
            # Route Qwen3-TTS: Modal if run_in_modal True; else gateway local qwen3_tts
            if run_in_modal:
                return (
                    await self._post_modal_tts(
                        text, voice_id, language, speed=speed, emotion=emotion, backend=backend
                    ),
                    False,
                )
            # Route to gateway local qwen3_tts engine
            resolved_voice = await resolve_gateway_qwen_voice(voice_id, language)
            payload: dict[str, Any] = {
                "text": text,
                "voice_id": resolved_voice,
                "language": language,
                "speed": speed,
                "engine": "qwen3_tts",
            }
            if emotion:
                payload["emotion"] = emotion
            try:
                return await self._post_audio("tts", payload), False
            except NarrationGatewayError as exc:
                fallback_allowed = (
                    self._fallback_to_edge if allow_edge_fallback is None else allow_edge_fallback
                )
                if not fallback_allowed:
                    raise
                edge_voice = edge_voice_for(voice_id, language)
                logger.warning(
                    "narration_gateway_fallback_to_edge",
                    voice_id=voice_id,
                    edge_voice=edge_voice,
                    status_code=exc.status_code,
                    error_type=exc.error_type,
                    error=str(exc),
                )
                try:
                    return await self._edge_synthesize(text, edge_voice, speed=speed), True
                except Exception as edge_exc:
                    raise NarrationGatewayError(
                        f"gateway failed ({exc}) and the Edge fallback failed ({edge_exc})"
                    ) from edge_exc
        if backend is not TTSBackend.OMNIVOICE:
            return (
                await self._post_modal_tts(
                    text, voice_id, language, speed=speed, emotion=emotion, backend=backend
                ),
                False,
            )
        payload: dict[str, Any] = {
            "text": text,
            "voice_id": voice_id,
            "language": language,
            "speed": speed,
        }
        if emotion:
            payload["emotion"] = emotion
        try:
            return await self._post_audio("tts", payload), False
        except NarrationGatewayError as exc:
            fallback_allowed = (
                self._fallback_to_edge if allow_edge_fallback is None else allow_edge_fallback
            )
            if not fallback_allowed:
                raise
            edge_voice = edge_voice_for(voice_id, language)
            logger.warning(
                "narration_gateway_fallback_to_edge",
                voice_id=voice_id,
                edge_voice=edge_voice,
                status_code=exc.status_code,
                error_type=exc.error_type,
                error=str(exc),
            )
            try:
                return await self._edge_synthesize(text, edge_voice, speed=speed), True
            except Exception as edge_exc:
                raise NarrationGatewayError(
                    f"gateway failed ({exc}) and the Edge fallback failed ({edge_exc})"
                ) from edge_exc

    async def synthesize_conversation(
        self, turns: list[dict[str, Any]], pause_ms: int, speed: float = 1.0
    ) -> bytes:
        """Render a conversation as several gateway requests, one per block.

        Only safe for homogeneous casts: the gateway concatenates the raw frames
        of every turn using the *last* turn's sample rate, so mixed engines would
        come out at the wrong pitch. Use :meth:`render`, which enforces the
        homogeneity rule before calling this.

        The whole dialogue would be a single request, which risks the request
        timeout on CPU and loses everything when one turn fails. Instead the
        turns are split into blocks (see :func:`split_turns_into_blocks`), each
        block is rendered on its own, and the canonical WAVs are joined with the
        same pause the gateway inserts between turns, so a block boundary sounds
        like any other turn change.

        Args:
            turns: Turn dicts with 'voice_id', 'text' and 'language'.
            pause_ms: Silence inserted between turns and between blocks, in
                milliseconds. The gateway does not scale these with ``speed``.
            speed: Speaking rate applied to every turn, in [0.5, 2.0].

        Returns:
            A single WAV payload in canonical format.

        Raises:
            NarrationGatewayError: If the gateway rejects every attempt of a
                block.
        """
        blocks = split_turns_into_blocks(
            turns,
            max_chars=self._conv_max_chars,
            max_turns=self._conv_max_turns,
            min_turns=self._conv_min_turns,
        )
        logger.info(
            "narration_conversation_blocks_rendered",
            blocks=len(blocks),
            turns=len(turns),
        )
        parts: list[tuple[bytes, int]] = []
        for index, block in enumerate(blocks):
            audio = await self._post_conversation_block(
                {"turns": block, "pause_ms": pause_ms, "speed": speed}, index=index
            )
            parts.append((await self._to_canonical(audio), pause_ms))
        return concat_wavs(parts)

    async def _post_conversation_block(self, payload: dict[str, Any], *, index: int) -> bytes:
        """POST one conversation block, retrying on gateway failure.

        The conversations strategy has no Edge fallback, so a single failed turn
        would otherwise discard the whole render. Retrying just the failed block
        keeps a transient gateway error from throwing away minutes of audio.

        Args:
            payload: The block payload for the gateway.
            index: Block position, for logging.

        Returns:
            The raw WAV payload from the gateway.

        Raises:
            NarrationGatewayError: If every attempt fails.
        """
        attempts = self._conv_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return await self._post_audio("conversations", payload)
            except NarrationGatewayError as exc:
                if attempt >= attempts:
                    raise
                logger.warning(
                    "narration_conversation_block_retry",
                    block=index,
                    attempt=attempt,
                    error=str(exc),
                )
        raise NarrationGatewayError(f"conversation block {index} failed")

    async def _edge_synthesize(self, text: str, voice: str, *, speed: float = 1.0) -> bytes:
        """Synthesize text with edge-tts and convert the MP3 to canonical WAV.

        Edge reads the speaking rate as a percentage offset of the voice's
        natural tempo, so ``speed`` is translated instead of ignored: a
        narration that asks for 1.2x must not come out at 1.0x just because the
        engine changed.

        Args:
            text: Speech text.
            voice: Valid Edge neural ShortName.
            speed: Speaking rate in [0.5, 2.0].

        Returns:
            Canonical WAV bytes.

        Raises:
            NarrationGatewayError: If Edge yields no audio or times out.
        """
        kwargs: dict[str, Any] = {}
        if speed != 1.0:
            kwargs["rate"] = f"{round((speed - 1.0) * 100):+d}%"
        communicate = edge_tts.Communicate(text, voice, **kwargs)
        chunks: list[bytes] = []

        async def _collect() -> None:
            async for item in communicate.stream():
                if item["type"] == "audio":
                    chunks.append(item["data"])

        try:
            await asyncio.wait_for(_collect(), timeout=self._timeout)
        except TimeoutError as exc:
            raise NarrationGatewayError(f"Edge TTS timed out after {self._timeout}s") from exc
        if not chunks:
            raise NarrationGatewayError(f"Edge TTS produced no audio for voice {voice}")
        return await self._ffmpeg_bytes(
            ["-i", "pipe:0", "-f", "wav", *self._canonical_output_args()], b"".join(chunks)
        )

    # --- ffmpeg helpers -----------------------------------------------------

    @staticmethod
    def _canonical_output_args() -> list[str]:
        """Return the ffmpeg output arguments for the canonical format.

        Returns:
            Argument list producing mono 16-bit PCM at 24 kHz.
        """
        return [
            "-ac",
            str(CANONICAL_CHANNELS),
            "-ar",
            str(CANONICAL_RATE),
            "-c:a",
            "pcm_s16le",
        ]

    async def _ffmpeg_bytes(self, args: list[str], data: bytes, timeout: int = 300) -> bytes:
        """Run ffmpeg with an in-memory input and read its output file.

        The output goes to a temporary file instead of a pipe on purpose: when
        ffmpeg writes a WAV to a non-seekable stream it cannot patch the RIFF
        sizes afterwards and leaves them as ``0xFFFFFFFF`` placeholders, which
        makes the resulting audio look hours long.

        Args:
            args: ffmpeg arguments (without the leading binary, ``-y`` and the
                output target, which this method appends).
            data: Bytes fed to ffmpeg on stdin.
            timeout: Seconds before the process is killed.

        Returns:
            The bytes ffmpeg wrote to the output file.

        Raises:
            NarrationRenderError: If ffmpeg is missing, times out or fails.
        """
        descriptor, target = tempfile.mkstemp(suffix=".out")
        os.close(descriptor)
        command = [self._ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args, target]
        try:
            try:
                result = await asyncio.to_thread(
                    subprocess.run,
                    command,
                    input=data,
                    capture_output=True,
                    timeout=timeout,
                )
            except FileNotFoundError as exc:
                raise NarrationRenderError(f"ffmpeg not found at {self._ffmpeg}") from exc
            except subprocess.TimeoutExpired as exc:
                raise NarrationRenderError("ffmpeg timed out") from exc
            if result.returncode != 0:
                detail = (result.stderr or b"").decode(errors="replace").strip() or "ffmpeg failed"
                raise NarrationRenderError(f"ffmpeg error: {detail}")
            return Path(target).read_bytes()
        finally:
            os.unlink(target)

    async def _to_canonical(self, data: bytes) -> bytes:
        """Resample a WAV payload to the canonical narration format.

        Args:
            data: Raw WAV bytes.

        Returns:
            WAV bytes in canonical format (returned untouched when already so).
        """
        fmt = wav_format(data)
        if fmt == CANONICAL_FORMAT:
            return data
        logger.info("narration_resampling", **{"from": str(fmt), "to": str(CANONICAL_FORMAT)})
        converted = await self._ffmpeg_bytes(
            ["-i", "pipe:0", "-f", "wav", *self._canonical_output_args()], data
        )
        return converted

    async def _mix_music(self, voice_wav: bytes, bed: MusicBed) -> bytes:
        """Lay a local music track under the narration.

        The bed is looped to the narration length, faded in and out, and ducked
        with a sidechain compressor so it never masks the voice. Music is
        cosmetic: if ffmpeg fails the narration is returned unmuted instead of
        losing the whole render.

        Args:
            voice_wav: Canonical narration WAV.
            bed: Music bed configuration.

        Returns:
            Mixed canonical WAV bytes.

        Raises:
            NarrationRenderError: If the configured track does not exist.
        """
        track = (self._music_dir / bed.track).resolve()
        if self._music_dir.resolve() not in track.parents or not track.is_file():
            raise NarrationRenderError(f"music track not found: {bed.track}")

        duration = wav_duration(voice_wav)
        fade_out_start = max(0.0, duration - bed.fade_out_ms / 1000)
        # Two guards keep the mix exactly as long as the narration: the bed is
        # trimmed (it is looped with -stream_loop -1) and the output is capped.
        # Without them the output length depends on ffmpeg being able to infer
        # the voice duration, which it cannot when the voice arrives on a pipe.
        #
        # asplit is also mandatory: a filter output can only be consumed once,
        # and the voice is needed both as the mix main input and as the
        # sidechain key. The sidechain compressor shapes the dip and duck_db
        # applies the configured extra attenuation on top of it.
        # The sidechain compressor shapes the dip and duck_db applies the
        # configured extra attenuation on top of it, in its own chain: a filter
        # cannot be appended after a labelled output.
        duck = f"[ducked]volume={bed.duck_db}dB[ducked2];" if bed.duck_db else ""
        mix_input = "ducked2" if bed.duck_db else "ducked"
        graph = (
            f"[0:a]aformat=sample_fmts=s16:sample_rates={CANONICAL_RATE}"
            f":channel_layouts=mono,asplit=2[vmain][vsc];"
            f"[1:a]aformat=sample_fmts=fltp:sample_rates={CANONICAL_RATE}"
            f":channel_layouts=mono,volume={bed.volume_db}dB,"
            f"afade=t=in:st=0:d={bed.fade_in_ms / 1000},"
            f"afade=t=out:st={fade_out_start:.3f}:d={bed.fade_out_ms / 1000},"
            f"atrim=end={duration:.3f},asetpts=N/SR/TB[bed];"
            f"[bed][vsc]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=300"
            f":makeup=1[ducked];"
            f"{duck}"
            f"[vmain][{mix_input}]amix=inputs=2:duration=first:normalize=0[out]"
        )
        try:
            return await self._ffmpeg_bytes(
                [
                    "-i",
                    "pipe:0",
                    "-stream_loop",
                    "-1",
                    "-i",
                    str(track),
                    "-filter_complex",
                    graph,
                    "-map",
                    "[out]",
                    "-t",
                    f"{duration:.3f}",
                    "-f",
                    "wav",
                    *self._canonical_output_args(),
                ],
                voice_wav,
            )
        except NarrationRenderError as exc:
            logger.warning("narration_music_mix_failed", track=bed.track, error=str(exc))
            return voice_wav

    # --- Render -------------------------------------------------------------

    def _turns_for_conversation(
        self, spec: NarrationSpec, script: NarrationScript
    ) -> tuple[list[dict[str, Any]], float]:
        """Build the turn list and speaking rate for the single-request strategy.

        Args:
            spec: Narration spec.
            script: Script to render.

        Returns:
            Turn list ready for ``/conversations`` and the single speaking
            rate shared by every turn.

        Raises:
            NarrationRenderError: If the cast needs per-turn emotion, needs
                different speaking rates per turn, or has a single turn, none of
                which the endpoint can honor.
        """
        if len(script.segments) < MIN_CONVERSATION_TURNS:
            raise NarrationRenderError(
                "the conversations endpoint needs at least "
                f"{MIN_CONVERSATION_TURNS} turns, got {len(script.segments)}"
            )
        turns: list[dict[str, Any]] = []
        speeds: set[float] = set()
        for segment in script.segments:
            role = spec.role(segment.role)
            emotion = segment.emotion or role.emotion or spec.emotion
            if emotion:
                raise NarrationRenderError(
                    "the conversations endpoint ignores emotions; use the turns "
                    f"strategy for segment {segment.role!r}"
                )
            speeds.add(role.speed if role.speed != 1.0 else spec.speed)
            for chunk in chunk_text(strip_emotion_tags(segment.text), self._max_chars):
                turns.append(
                    {
                        "voice_id": role.voice_id,
                        "text": chunk,
                        "language": role.language or spec.language,
                    }
                )
        if len(speeds) > 1:
            # The endpoint takes one speed for the whole request, so a
            # heterogeneous cast would be rendered at one arbitrary rate: fail
            # loudly instead.
            raise NarrationRenderError(
                "the conversations endpoint applies a single speaking rate, got "
                f"{sorted(speeds)}; use the turns strategy for per-role speeds"
            )
        return turns, speeds.pop()

    @staticmethod
    def _used_voices(spec: NarrationSpec, script: NarrationScript) -> list[str]:
        """Return the voices the script actually uses, in order of first use.

        Args:
            spec: Narration spec.
            script: Script to render.

        Returns:
            Voice ids without duplicates, in the order they first appear.
        """
        voices: list[str] = []
        for segment in script.segments:
            voice_id = spec.role(segment.role).voice_id
            if voice_id not in voices:
                voices.append(voice_id)
        return voices

    @staticmethod
    def _is_dialog(
        spec: NarrationSpec,
        script: NarrationScript,
        strategy: RenderStrategy,
    ) -> bool:
        """Determine if a render counts as a dialog/podcast with multiple voices.

        Args:
            spec: Narration spec.
            script: Script to render.
            strategy: Render strategy.

        Returns:
            True if the render uses the conversations strategy or actually
            speaks with two or more distinct voices.
        """
        if strategy == RenderStrategy.CONVERSATIONS:
            return True
        used = NarrationTTSService._used_voices(spec, script)
        return len(used) >= 2

    async def _apply_atempo(self, audio: bytes, speed: float) -> bytes:
        """Change the playback rate of a WAV while preserving pitch.

        Qwen3-TTS Base has no speed control, so the rate is adjusted in
        post-processing. ffmpeg's atempo keeps the pitch intact, unlike a
        resampler, which would shift it.

        Args:
            audio: Canonical WAV bytes.
            speed: Speaking rate multiplier in [0.5, 2.0].

        Returns:
            The time-stretched WAV bytes.

        Raises:
            NarrationRenderError: When ffmpeg is missing or fails.
        """
        if speed == 1.0:
            return audio
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.wav"
            target = Path(tmp) / "out.wav"
            source.write_bytes(audio)
            command = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-filter:a",
                f"atempo={speed}",
                str(target),
            ]
            try:
                result = await asyncio.to_thread(
                    subprocess.run, command, capture_output=True, text=True, timeout=300
                )
            except FileNotFoundError as exc:
                raise NarrationRenderError("ffmpeg not found for atempo") from exc
            if result.returncode != 0:
                raise NarrationRenderError(f"atempo error: {result.stderr.strip()}")
            return target.read_bytes()

    async def _render_turns_locally(
        self,
        spec: NarrationSpec,
        script: NarrationScript,
        allow_fallback: bool,
    ) -> tuple[bytes, int]:
        """Synthesize every turn separately and assemble them locally.

        This is the default path: it honors per-turn speed, emotion and pauses,
        and resamples engines that disagree on the sample rate.

        Args:
            spec: Narration spec.
            script: Script to render.
            allow_fallback: Whether to allow Edge fallback for this render.

        Returns:
            Tuple of ``(canonical_wav_bytes, edge_fallback_count)``.
        """
        parts: list[tuple[bytes, int]] = []
        fallbacks = 0
        for segment in script.segments:
            role = spec.role(segment.role)
            language = role.language or spec.language
            speed = role.speed if role.speed != 1.0 else spec.speed
            emotion = segment.emotion or role.emotion or spec.emotion
            text = strip_emotion_tags(segment.text)
            for chunk in chunk_text(text, self._max_chars):
                audio, used_edge = await self.synthesize_turn(
                    chunk,
                    role.voice_id,
                    language,
                    speed=speed,
                    emotion=emotion,
                    allow_edge_fallback=allow_fallback,
                    backend=spec.tts_backend,
                    run_in_modal=getattr(spec, "run_in_modal", False),
                )
                fallbacks += int(used_edge)
                audio = await self._to_canonical(audio)
                if spec.tts_backend is TTSBackend.QWEN3_TTS and speed != 1.0:
                    audio = await self._apply_atempo(audio, speed)
                parts.append((audio, 0))
            pause_ms = (
                segment.pause_after_ms if segment.pause_after_ms is not None else spec.pause_ms
            )
            parts[-1] = (parts[-1][0], pause_ms)
        return concat_wavs(parts), fallbacks

    @staticmethod
    def _with_disclosure(script: NarrationScript, disclosure: str) -> NarrationScript:
        """Return a copy of the script with the AI disclosure prepended.

        The disclosure is spoken by the same voice as the first segment, so it
        needs no extra synthesis call. It is part of the rendered text and
        therefore part of the content hash.

        Args:
            script: Script to annotate.
            disclosure: Audible disclosure text.

        Returns:
            A new script whose first segment opens with the disclosure.
        """
        first = script.segments[0]
        segments = [first.model_copy(update={"text": f"{disclosure} {first.text}"})]
        segments.extend(script.segments[1:])
        return script.model_copy(update={"segments": segments})

    def cache_probe(
        self,
        spec: NarrationSpec,
        script: NarrationScript,
        collection: str = "default",
        day: datetime | None = None,
        strategy: RenderStrategy = RenderStrategy.TURNS,
    ) -> NarrationOut | None:
        """Return a cached render when one is still usable, else None.

        A cache hit is free: no synthesis, no gateway call and no Modal spend.
        The budget guard in the Modal activity calls this before estimating
        cost, so an exhausted budget never blocks audio that is already on disk.

        A cached entry whose audio is inaudible is discarded rather than served,
        so the caller re-renders instead of shipping an empty podcast.

        Args:
            spec: Narration spec.
            script: Script to render.
            collection: Collection name owning the narration.
            day: Optional datetime driving the cache filename.
            strategy: Render strategy; different strategies produce different audio.

        Returns:
            The cached render, or None when the caller must render.
        """
        script = NarrationScript.model_validate(script.model_dump(), context={"spec": spec})
        key = content_hash(spec, script, strategy)
        wav_path = self._cache_dir / narration_filename(collection, day, key)
        meta_path = sidecar_path(wav_path)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        meta = read_sidecar(meta_path)
        if not (wav_path.exists() and meta and meta.get("key") == key):
            return None
        m4a_path = Path(str(wav_path.with_suffix(".m4a")))
        if not (m4a_path.exists() and is_audible(wav_path.read_bytes())):
            if m4a_path.exists():
                # A cached entry that would ship an empty podcast: drop it so
                # the render below runs again instead of serving it forever.
                logger.warning("narration_cache_entry_discarded", wav_path=str(wav_path))
                for stale in (wav_path, m4a_path, meta_path):
                    stale.unlink(missing_ok=True)
            return None
        logger.info("narration_cache_hit", audio_path=str(m4a_path))
        return NarrationOut(
            audio_path=str(m4a_path),
            wav_path=str(wav_path),
            duration_seconds=float(meta.get("duration_seconds", 0.0)),
            cached=True,
            segments=int(meta.get("segments", len(script.segments))),
            char_count=script.total_chars(),
            voices=list(meta.get("voices", [])),
            used_conversations_api=bool(meta.get("used_conversations_api", False)),
            edge_fallbacks=int(meta.get("edge_fallbacks", 0)),
            conversation_blocks=int(meta.get("conversation_blocks", 1)),
            music=meta.get("music"),
            title=script.title or spec.title,
            tts_backend=str(meta.get("tts_backend", "")),
            voice_source=str(meta.get("voice_source", "")),
            ai_disclosure=bool(meta.get("ai_disclosure", False)),
        )

    async def render(
        self,
        spec: NarrationSpec,
        script: NarrationScript,
        collection: str = "default",
        day: datetime | None = None,
        strategy: RenderStrategy = RenderStrategy.TURNS,
    ) -> NarrationOut:
        """Render a script to a cached M4A, reusing a previous identical render.

        Args:
            spec: Narration spec (cast, delivery defaults, music bed).
            script: Script to render.
            collection: Collection name owning the narration.
            day: Optional datetime driving the cache filename. Defaults to now (UTC).
            strategy: Render strategy. Defaults to one request per turn.

        Returns:
            The render result, pointing at the M4A to deliver.

        Raises:
            NarrationGatewayError: If synthesis fails and no fallback is possible.
            NarrationRenderError: If audio cannot be assembled, mixed or written,
                or if the assembled audio is inaudible.
        """
        disclosure = get_settings().narration_ai_disclosure_text
        if spec.ai_disclosure and disclosure:
            script = self._with_disclosure(script, disclosure)

        cached = self.cache_probe(spec, script, collection=collection, day=day, strategy=strategy)
        if cached is not None:
            return cached

        key = content_hash(spec, script, strategy)
        wav_path = self._cache_dir / narration_filename(collection, day, key)
        meta_path = sidecar_path(wav_path)

        used_voices = self._used_voices(spec, script)
        used_conversations = strategy == RenderStrategy.CONVERSATIONS
        if used_conversations and spec.tts_backend is not TTSBackend.OMNIVOICE:
            raise NarrationRenderError(
                "conversations strategy is only supported by the omni_voice backend"
            )
        is_dialog = self._is_dialog(spec, script, strategy)
        allow_fallback = self._dialog_fallback_to_edge if is_dialog else self._fallback_to_edge
        if is_dialog and not allow_fallback:
            logger.info(
                "narration_dialog_fallback_denied",
                collection=collection,
                used_voices=used_voices,
                strategy=str(strategy),
            )
        conversation_blocks = 1
        if used_conversations:
            turns, speed = self._turns_for_conversation(spec, script)
            conversation_blocks = len(
                split_turns_into_blocks(
                    turns,
                    max_chars=self._conv_max_chars,
                    max_turns=self._conv_max_turns,
                    min_turns=self._conv_min_turns,
                )
            )
            audio = await self._to_canonical(
                await self.synthesize_conversation(turns, spec.pause_ms, speed=speed)
            )
            fallbacks = 0
        else:
            audio, fallbacks = await self._render_turns_locally(spec, script, allow_fallback)

        if spec.music is not None:
            audio = await self._mix_music(audio, spec.music)

        duration = wav_duration(audio)
        peak = wav_peak_ratio(audio)
        if peak is not None and peak < SILENCE_PEAK_RATIO:
            raise NarrationRenderError(
                "narration audio is inaudible: "
                f"peak={peak:.6f} of full scale after {duration}s "
                f"({len(script.segments)} segments, collection={collection})"
            )
        atomic_write(wav_path, audio)
        m4a_path = Path(await self._compressor.compress(str(wav_path)))
        atomic_write(
            meta_path,
            json.dumps(
                {
                    "version": SIDECAR_VERSION,
                    "key": key,
                    "collection": collection,
                    "title": script.title or spec.title,
                    "language": spec.language,
                    "segments": len(script.segments),
                    "char_count": script.total_chars(),
                    "duration_seconds": duration,
                    "voices": used_voices,
                    "used_conversations_api": used_conversations,
                    "edge_fallbacks": fallbacks,
                    "conversation_blocks": conversation_blocks,
                    "music": spec.music.track if spec.music else None,
                    "tts_backend": str(spec.tts_backend),
                    "voice_source": str(spec.voice_source),
                    "ai_disclosure": spec.ai_disclosure,
                    "generated_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
                sort_keys=True,
            ).encode(),
        )
        logger.info(
            "narration_rendered",
            audio_path=str(m4a_path),
            duration_seconds=duration,
            segments=len(script.segments),
            used_conversations_api=used_conversations,
            edge_fallbacks=fallbacks,
        )
        return NarrationOut(
            audio_path=str(m4a_path),
            wav_path=str(wav_path),
            duration_seconds=duration,
            cached=False,
            segments=len(script.segments),
            char_count=script.total_chars(),
            voices=used_voices,
            used_conversations_api=used_conversations,
            edge_fallbacks=fallbacks,
            conversation_blocks=conversation_blocks,
            music=spec.music.track if spec.music else None,
            title=script.title or spec.title,
            tts_backend=str(spec.tts_backend),
            voice_source=str(spec.voice_source),
            ai_disclosure=spec.ai_disclosure,
        )
