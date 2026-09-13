"""OmniVoice voices service for the admin GUI.

Provides stock voice listing (GET /voices/stock), cloned voice listing
(GET /voices/cloned), and voice cloning (POST /voices/clone) against the
external OmniVoice TTS API. Responses are cached for a short TTL to keep the
GUI snappy without hammering the TTS backend.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from src.core.config import get_settings

logger = structlog.get_logger(__name__)

CACHE_TTL_SECONDS = 60


def _headers(settings: Any) -> dict[str, str]:
    """Build the OmniVoice request headers.

    Args:
        settings: Application settings.

    Returns:
        Header dict, including X-API-Key when configured.
    """
    headers = {"Accept": "application/json"}
    if settings.tts_api_key:
        headers["X-API-Key"] = settings.tts_api_key
    return headers


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

    async def _get(self, path: str) -> Any:
        """GET a JSON endpoint of the OmniVoice API.

        Args:
            path: Path appended to the base URL (e.g. 'voices/stock').

        Returns:
            Parsed JSON payload.

        Raises:
            httpx.HTTPError: On transport or non-2xx errors.
        """
        settings = get_settings()
        url = f"{self._api_base_url}/{path}"
        async with httpx.AsyncClient(timeout=settings.api_request_timeout) as client:
            response = await client.get(url, headers=_headers(settings))
            response.raise_for_status()
            return response.json()

    async def list_stock(
        self, language: str | None = None, force: bool = False
    ) -> list[dict[str, Any]]:
        """List available stock voices, optionally filtered by language.

        Args:
            language: ISO 639-1 filter (e.g. 'es', 'en').
            force: Bypass the short-TTL cache.

        Returns:
            List of stock voice dicts with voice_id / language / gender / name.
        """
        now = time.monotonic()
        if force or (now - self._cache_ts) > CACHE_TTL_SECONDS or not self._stock_cache:
            try:
                data = await self._get("voices/stock")
                if isinstance(data, list):
                    self._stock_cache = [dict(item) for item in data]
                    self._cache_ts = now
            except httpx.HTTPError as exc:
                logger.warning("voices_stock_fetch_failed", error=str(exc))
        voices = self._stock_cache
        if language:
            voices = [v for v in voices if v.get("language") == language]
        return voices

    async def list_cloned(self, force: bool = False) -> list[dict[str, Any]]:
        """List cloned voices.

        Args:
            force: Bypass the short-TTL cache.

        Returns:
            List of cloned voice dicts.
        """
        now = time.monotonic()
        if force or (now - self._cache_ts) > CACHE_TTL_SECONDS or not self._cloned_cache:
            try:
                data = await self._get("voices/cloned")
                if isinstance(data, list):
                    self._cloned_cache = [dict(item) for item in data]
                    self._cache_ts = now
            except httpx.HTTPError as exc:
                logger.warning("voices_cloned_fetch_failed", error=str(exc))
        return self._cloned_cache

    async def clone_voice(
        self,
        name: str,
        language: str,
        reference_audio: bytes,
        filename: str = "reference.wav",
    ) -> dict[str, Any]:
        """Clone a voice from a reference audio file.

        Args:
            name: Unique voice name (3-64 chars, ^[a-zA-Z0-9_-]+$).
            language: ISO 639-1 language code.
            reference_audio: Raw audio bytes (WAV/FLAC, 5-30s).
            filename: Uploaded filename used in multipart form.

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
        data = {"name": name, "language": language}
        headers = _headers(settings)
        async with httpx.AsyncClient(timeout=settings.api_request_timeout) as client:
            response = await client.post(url, data=data, files=files, headers=headers)
            response.raise_for_status()
            return response.json()
