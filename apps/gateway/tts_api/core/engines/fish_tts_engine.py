"""Fish Audio cloud TTS engine.

Stock voices are referenced by a Fish ``reference_id`` configured in
``FISH_VOICES`` (or passed as the gateway voice id). Catalog voices clone their
reference WAV inline with a MessagePack request, which is the only Fish path
that accepts raw audio without a prior upload.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import msgpack

from tts_api.core.engine_base import EngineCapabilities, TtsEngineBase
from tts_api.core.exceptions import (
    EngineUnavailableError,
    FeatureNotSupportedError,
    UnsupportedLanguageError,
)
from tts_api.settings import get_settings

logger = logging.getLogger(__name__)


class FishTTSEngine(TtsEngineBase):
    """Fish Audio ``POST /v1/tts`` backend."""

    def __init__(self) -> None:
        settings = get_settings()
        self._base_url = settings.FISH_API_BASE_URL.strip().rstrip("/")
        self._api_key = settings.FISH_API_KEY.strip()
        self._model = settings.FISH_MODEL.strip() or "s2.1-pro-free"
        self._sample_rate = settings.FISH_SAMPLE_RATE
        self._timeout = settings.FISH_TIMEOUT
        self._inline_references = settings.FISH_USE_INLINE_REFERENCES
        self._voices = settings.fish_voices
        self._languages = settings.fish_languages
        self._client: httpx.AsyncClient | None = None
        self._available = False

    @property
    def name(self) -> str:
        return "fish_tts"

    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            stock_voices=True,
            voice_cloning=True,
            instruct_voice_design=False,
            emotions=False,
            streaming=False,
            supported_languages=list(self._languages),
            locations=["cloud"],
        )

    async def initialize(self) -> None:
        if not self._api_key:
            raise EngineUnavailableError(
                "FISH_API_KEY no configurada; el engine fish_tts queda desactivado"
            )
        if not self._base_url:
            raise EngineUnavailableError(
                "FISH_API_BASE_URL no configurada; el engine fish_tts queda desactivado"
            )
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)
        self._available = True
        logger.info("Fish Audio engine initialized (cloud, model=%s)", self._model)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._available = False

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None or not self._available:
            raise EngineUnavailableError(
                "Fish Audio no disponible; configura FISH_API_KEY y reinicia el gateway"
            )
        return self._client

    def _headers(self, content_type: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": content_type,
            "model": self._model,
        }

    async def _post(self, payload: dict[str, Any], *, messagepack: bool) -> bytes:
        client = self._require_client()
        body = msgpack.packb(payload, use_bin_type=True) if messagepack else None
        content_type = "application/msgpack" if messagepack else "application/json"
        try:
            response = await client.post(
                "/v1/tts",
                content=body if messagepack else None,
                json=None if messagepack else payload,
                headers=self._headers(content_type),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise EngineUnavailableError(
                f"Fish Audio rechazó /v1/tts: {exc.response.status_code} {exc.response.text[:300]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise EngineUnavailableError(f"Error llamando a Fish Audio: {exc}") from exc
        if not response.content:
            raise EngineUnavailableError("Fish Audio devolvió audio vacío")
        return response.content

    def _base_payload(self, *, text: str, language: str, speed: float) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "text": text,
            "format": "wav",
            "sample_rate": self._sample_rate,
            "language": language,
        }
        if speed != 1.0:
            payload["speed"] = speed
        return payload

    def _reference_id(self, voice_id: str) -> str:
        entry = self._voices.get(voice_id, {})
        return str(entry.get("reference_id") or entry.get("voice") or voice_id).strip()

    async def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
        engine_name: str | None = None,
    ) -> bytes:
        if language not in self._languages:
            raise UnsupportedLanguageError(language, list(self._languages))
        payload = self._base_payload(text=text, language=language, speed=speed)
        reference_id = self._reference_id(voice_id)
        if reference_id:
            payload["reference_id"] = reference_id
        return await self._post(payload, messagepack=False)

    async def synthesize_clone(
        self,
        *,
        text: str,
        reference_audio_path: str,
        language: str,
        speed: float = 1.0,
        ref_text: str | None = None,
        engine_name: str | None = None,
    ) -> bytes:
        if language not in self._languages:
            raise UnsupportedLanguageError(language, list(self._languages))
        if not self._inline_references:
            raise FeatureNotSupportedError("inline reference cloning", self.name)
        path = Path(reference_audio_path)
        if not path.is_file():
            raise EngineUnavailableError(f"Reference audio no encontrado: {reference_audio_path}")
        payload = self._base_payload(text=text, language=language, speed=speed)
        payload["references"] = [{"audio": path.read_bytes(), "text": ref_text or ""}]
        return await self._post(payload, messagepack=True)

    async def synthesize_instruct(
        self,
        *,
        text: str,
        instruct: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
        engine_name: str | None = None,
    ) -> bytes:
        raise FeatureNotSupportedError("instruct/voice design", self.name)

    async def list_stock_voices(self, language: str | None = None) -> list[dict[str, Any]]:
        voices = [
            {
                "voice_id": voice_id,
                "language": str(meta.get("language") or ""),
                "gender": str(meta.get("gender") or ""),
                "name": str(meta.get("name") or voice_id),
                "engine": self.name,
            }
            for voice_id, meta in self._voices.items()
        ]
        if language:
            voices = [voice for voice in voices if voice["language"] == language]
        return voices

    async def health_check(self) -> dict[str, Any]:
        configured = bool(self._api_key and self._base_url)
        return {
            "model_loaded": configured,
            "gpu_available": False,
            "device": "cloud",
            "stock_voices_count": len(self._voices),
            "vram_free_mb": 0,
            "mode": "REAL" if configured else "NOT_CONFIGURED",
            "engine": self.name,
            "model": self._model,
        }
