"""OmniVoice voices service for the admin GUI.

Provides stock voice listing (GET /voices/stock), cloned voice listing
(GET /voices/cloned), and voice cloning (POST /voices/clone) against the
external OmniVoice TTS API. Responses are cached for a short TTL to keep the
GUI snappy without hammering the TTS backend.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import time
from typing import Any

import httpx
import structlog
from tts_contract import ClonedVoice, CloneVoiceResponse, EnginesResponse, StockVoice

from src.api.services.gateway import gateway_headers
from src.core.config import get_settings

logger = structlog.get_logger(__name__)

CACHE_TTL_SECONDS = 60
_CATALOG_CACHE: dict[tuple[str, str, str, tuple[tuple[str, str], ...]], tuple[float, Any]] = {}
_CATALOG_INFLIGHT: dict[tuple[str, str, str, tuple[tuple[str, str], ...]], asyncio.Task[Any]] = {}


def _catalog_key(
    api_base_url: str, api_key: str, path: str, params: dict[str, str] | None
) -> tuple[str, str, str, tuple[tuple[str, str], ...]]:
    """Build a process-wide cache key without exposing the API key."""
    key_digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
    ordered = tuple(sorted((params or {}).items()))
    return api_base_url.rstrip("/"), key_digest, path, ordered


def reset_voice_catalog_cache() -> None:
    """Drop the shared catalog cache and its in-flight requests."""
    _CATALOG_CACHE.clear()
    _CATALOG_INFLIGHT.clear()


def _headers(settings: Any) -> dict[str, str]:
    """Build the OmniVoice request headers.

    The GUI only ever reads JSON, so it asks for JSON alone; the shared builder
    still owns the X-API-Key rule so no second copy can drift from the two
    synthesis clients.

    Args:
        settings: Application settings.

    Returns:
        Header dict, including X-API-Key when configured.
    """
    return gateway_headers(settings.tts_api_key, accept="application/json")


class VoicesService:
    """Handles voice discovery and cloning against OmniVoice."""

    def __init__(self, api_base_url: str | None = None, api_key: str = "") -> None:
        """Initialize the service with optional overrides.

        Args:
            api_base_url: OmniVoice base URL. Defaults to settings.tts_api_base_url.
            api_key: Optional API key. Defaults to settings.tts_api_key.
        """
        settings = get_settings()
        self._api_base_url = (api_base_url or settings.tts_api_base_url).rstrip("/")
        self._api_key = api_key or settings.tts_api_key
        self._cache_ts: float = 0.0
        self._stock_cache: list[dict[str, Any]] = []
        self._cloned_cache: list[dict[str, Any]] = []

    async def _get_shared(
        self,
        path: str,
        params: dict[str, str] | None = None,
        *,
        force: bool = False,
        ttl: float = CACHE_TTL_SECONDS,
    ) -> Any:
        """GET JSON with process-wide TTL caching and in-flight deduplication.

        Args:
            path: Path appended to the base URL.
            params: Optional query parameters.
            force: Bypass the fresh cached value, but still deduplicate concurrent
                callers asking for the same key.
            ttl: Cache lifetime in seconds; zero keeps only in-flight deduplication.

        Returns:
            Parsed JSON payload.
        """
        key = _catalog_key(self._api_base_url, self._api_key, path, params)
        now = time.monotonic()
        cached = _CATALOG_CACHE.get(key)
        if not force and cached is not None and (now - cached[0]) <= ttl:
            return copy.deepcopy(cached[1])

        task = _CATALOG_INFLIGHT.get(key)
        if task is None:
            task = asyncio.create_task(self._get(path, params=params))
            _CATALOG_INFLIGHT[key] = task
        try:
            data = await asyncio.shield(task)
            _CATALOG_CACHE[key] = (time.monotonic(), data)
            return copy.deepcopy(data)
        finally:
            if _CATALOG_INFLIGHT.get(key) is task:
                del _CATALOG_INFLIGHT[key]

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        """GET a JSON endpoint of the OmniVoice API.

        Args:
            path: Path appended to the base URL (e.g. 'voices/stock').
            params: Optional query parameters.

        Returns:
            Parsed JSON payload.

        Raises:
            httpx.HTTPError: On transport or non-2xx errors.
        """
        settings = get_settings()
        url = f"{self._api_base_url}/{path}"
        async with httpx.AsyncClient(timeout=settings.api_request_timeout) as client:
            response = await client.get(url, headers=_headers(settings), params=params)
            response.raise_for_status()
            return response.json()

    async def list_stock(
        self,
        language: str | None = None,
        force: bool = False,
        engine: str | None = None,
    ) -> list[dict[str, Any]]:
        """List available stock voices, optionally filtered by language and engine.

        Args:
            language: ISO 639-1 filter (e.g. 'es', 'en').
            force: Bypass the short-TTL cache.
            engine: Gateway engine id to ask for its own stock catalogue. ``None``
                keeps the historical behaviour (the gateway's active engine).

        Returns:
            List of stock voice dicts with voice_id / language / gender / name.
        """
        if engine:
            params = {"engine": engine}
            if language:
                params["language"] = language
            try:
                data = await self._get_shared("voices/stock", params=params, force=force)
                if isinstance(data, list):
                    return [
                        StockVoice.model_validate(item).model_dump()
                        for item in data
                        if isinstance(item, dict)
                    ]
            except httpx.HTTPError as exc:
                logger.warning("voices_engine_stock_fetch_failed", engine=engine, error=str(exc))
            return []
        try:
            data = await self._get_shared("voices/stock", force=force)
            if isinstance(data, list):
                self._stock_cache = [
                    StockVoice.model_validate(item).model_dump()
                    for item in data
                    if isinstance(item, dict)
                ]
                self._cache_ts = time.monotonic()
        except httpx.HTTPError as exc:
            logger.warning("voices_stock_fetch_failed", error=str(exc))
        voices = self._stock_cache
        if language:
            voices = [v for v in voices if v.get("language") == language]
        return voices

    async def get_engines(self) -> dict[str, Any]:
        """Return the gateway engine catalog (``GET /engines``).

        Returns:
            Dict with the ``engines`` list and the top-level ``active`` id.
            Empty lists when the gateway answers an unexpected shape.

        Raises:
            httpx.HTTPError: On transport or non-2xx errors.
        """
        data = await self._get("engines")
        if not isinstance(data, dict):
            return EnginesResponse().model_dump()
        return EnginesResponse.model_validate(data).model_dump()

    async def list_engines(self) -> list[dict[str, Any]]:
        """List the gateway engine descriptors (``GET /engines``).

        Returns:
            The ``engines`` list, each item with at least ``name`` and
            ``available``. Empty when the gateway answers an unexpected shape.

        Raises:
            httpx.HTTPError: On transport or non-2xx errors.
        """
        return (await self.get_engines())["engines"]

    async def list_languages(self, force: bool = False) -> list[str]:
        """List unique ISO 639-1 language codes across stock voices.

        Args:
            force: Bypass the short-TTL cache.

        Returns:
            Sorted list of unique language codes found in the stock voices
            (empty if OmniVoice is unreachable).
        """
        voices = await self.list_stock(force=force)
        languages = sorted({str(v["language"]).strip() for v in voices if v.get("language")})
        return languages

    async def list_cloned(self, force: bool = False) -> list[dict[str, Any]]:
        """List cloned voices.

        Args:
            force: Bypass the short-TTL cache.

        Returns:
            List of cloned voice dicts.
        """
        try:
            data = await self._get_shared("voices/cloned", force=force, ttl=0)
            if isinstance(data, list):
                self._cloned_cache = [
                    ClonedVoice.model_validate(item).model_dump()
                    for item in data
                    if isinstance(item, dict)
                ]
                self._cache_ts = time.monotonic()
        except httpx.HTTPError as exc:
            logger.warning("voices_cloned_fetch_failed", error=str(exc))
        return self._cloned_cache

    async def clone_voice(
        self,
        name: str,
        language: str,
        reference_audio: bytes,
        filename: str = "reference.wav",
        ref_text: str | None = None,
        engines: str = "auto",
    ) -> dict[str, Any]:
        """Clone a voice from a reference audio file.

        Args:
            name: Unique voice name (3-64 chars, ^[a-zA-Z0-9_-]+$).
            language: ISO 639-1 language code.
            reference_audio: Raw audio bytes (WAV/FLAC, 5-30s).
            filename: Uploaded filename used in multipart form.
            ref_text: Transcription of the reference audio (optional, improves cloning).
            engines: Comma-separated engine(s) to pin the clone on (e.g.
                'qwen3_tts'), or 'auto' to use the active engine router.

        Returns:
            The cloning response dict (voice_id, name, language).

        Raises:
            httpx.HTTPStatusError: On non-2xx (409 duplicate, 422 invalid audio).
        """
        settings = get_settings()
        url = f"{self._api_base_url}/voices/clone"
        files = {
            "reference_audio": (filename, reference_audio, "audio/wav"),
        }
        data = {"name": name, "language": language, "engines": engines}
        if ref_text:
            data["ref_text"] = ref_text
        headers = _headers(settings)
        async with httpx.AsyncClient(timeout=settings.api_request_timeout) as client:
            response = await client.post(url, data=data, files=files, headers=headers)
            response.raise_for_status()
            return CloneVoiceResponse.model_validate(response.json()).model_dump()
