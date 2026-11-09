"""Generic OpenAI-compatible ``POST /audio/speech`` TTS engine."""

from __future__ import annotations

import io
import logging
from typing import Any

import httpx

from tts_api.core.engine_base import EngineCapabilities, TtsEngineBase
from tts_api.core.exceptions import (
    EngineUnavailableError,
    FeatureNotSupportedError,
    UnsupportedLanguageError,
)
from tts_api.settings import get_settings

logger = logging.getLogger(__name__)


class OpenAITTSEngine(TtsEngineBase):
    """Stock-voice TTS against any OpenAI-compatible speech endpoint."""

    def __init__(self) -> None:
        settings = get_settings()
        self._base_url = settings.OPENAI_TTS_BASE_URL.strip().rstrip("/")
        self._api_key = settings.OPENAI_TTS_API_KEY.strip()
        self._model = settings.OPENAI_TTS_MODEL.strip()
        self._timeout = settings.OPENAI_TTS_TIMEOUT
        self._voices = settings.openai_tts_voices
        self._languages = settings.openai_tts_languages
        self._client: httpx.AsyncClient | None = None
        self._available = False

    @property
    def name(self) -> str:
        return "openai_tts"

    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            stock_voices=True,
            voice_cloning=False,
            instruct_voice_design=False,
            emotions=False,
            streaming=False,
            supported_languages=list(self._languages),
            locations=["cloud"],
        )

    async def initialize(self) -> None:
        if not self._base_url:
            raise EngineUnavailableError(
                "OPENAI_TTS_BASE_URL no configurada; el engine openai_tts queda desactivado"
            )
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)
        self._available = True
        logger.info("OpenAI-compatible TTS engine initialized (model=%s)", self._model)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._available = False

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None or not self._available:
            raise EngineUnavailableError(
                "OpenAI-compatible TTS no disponible; configura OPENAI_TTS_BASE_URL"
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _native_voice(self, voice_id: str) -> str:
        entry = self._voices.get(voice_id, {})
        return str(entry.get("voice") or voice_id).strip()

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
        payload: dict[str, Any] = {
            "model": self._model,
            "input": text,
            "voice": self._native_voice(voice_id),
            "response_format": "wav",
        }
        if speed != 1.0:
            payload["speed"] = speed
        client = self._require_client()
        try:
            response = await client.post("/audio/speech", json=payload, headers=self._headers())
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise EngineUnavailableError(
                f"OpenAI-compatible TTS rechazó /audio/speech: "
                f"{exc.response.status_code} {exc.response.text[:300]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise EngineUnavailableError(f"Error llamando al proveedor TTS: {exc}") from exc
        if not response.content:
            raise EngineUnavailableError("El proveedor TTS devolvió audio vacío")
        return self._to_wav(response.content)

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
        raise FeatureNotSupportedError("voice cloning", self.name)

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
        configured = bool(self._base_url)
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

    def _to_wav(self, audio: bytes) -> bytes:
        if audio[:4] == b"RIFF" and audio[8:12] == b"WAVE":
            return audio
        try:
            from pydub import AudioSegment

            segment = AudioSegment.from_file(io.BytesIO(audio))
            segment = segment.set_frame_rate(24000).set_channels(1).set_sample_width(2)
            buffer = io.BytesIO()
            segment.export(buffer, format="wav")
            return buffer.getvalue()
        except Exception as exc:
            raise EngineUnavailableError(
                f"No se pudo convertir la respuesta del proveedor a WAV: {exc}"
            ) from exc
