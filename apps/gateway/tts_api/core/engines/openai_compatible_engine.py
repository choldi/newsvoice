from __future__ import annotations

import io
import json
import logging
import os
from typing import Any

import httpx

from tts_api.core.engine_base import EngineCapabilities, TtsEngineBase
from tts_api.core.engine_registry import EngineSpec
from tts_api.core.exceptions import (
    EngineUnavailableError,
    FeatureNotSupportedError,
    UnsupportedLanguageError,
)

logger = logging.getLogger(__name__)


def _as_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip().lower() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip().lower() for item in value.split(",") if item.strip()]
    return []


def _as_voice_map(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        return {}
    voices: dict[str, dict[str, str]] = {}
    for raw_name, raw_meta in value.items():
        voice_id = str(raw_name).strip()
        if not voice_id:
            continue
        if isinstance(raw_meta, str):
            voices[voice_id] = {"voice": raw_meta.strip()}
        elif isinstance(raw_meta, dict):
            voices[voice_id] = {
                str(key): str(item) for key, item in raw_meta.items() if item is not None
            }
    return voices


class OpenAICompatibleEngine(TtsEngineBase):
    """Stock-voice TTS for declarative OpenAI-compatible providers."""

    def __init__(self, spec: EngineSpec) -> None:
        self._spec = spec
        options = spec.options
        self._base_url_env = str(options.get("base_url_env", "OPENAI_TTS_BASE_URL"))
        self._api_key_env = str(options.get("api_key_env", "OPENAI_TTS_API_KEY"))
        self._model_env = str(options.get("model_env", ""))
        self._model = str(options.get("model", "tts-1"))
        self._timeout_env = str(options.get("timeout_env", ""))
        self._timeout = float(options.get("timeout", 120.0))
        self._languages = _as_list(options.get("languages", []))
        self._voices = _as_voice_map(options.get("voices", {}))
        languages_env = str(options.get("languages_env", ""))
        voices_env = str(options.get("voices_env", ""))
        if languages_env and os.getenv(languages_env):
            self._languages = _as_list(os.getenv(languages_env))
        if voices_env and os.getenv(voices_env):
            try:
                self._voices = _as_voice_map(json.loads(os.getenv(voices_env) or "{}"))
            except json.JSONDecodeError:
                logger.warning("Invalid JSON in %s for engine %s", voices_env, spec.name)
        self._client: httpx.AsyncClient | None = None
        self._available = False

    @property
    def name(self) -> str:
        return self._spec.name

    @property
    def capabilities(self) -> EngineCapabilities:
        declared = self._spec.capabilities
        return EngineCapabilities(
            stock_voices=declared.stock_voices,
            voice_cloning=declared.voice_cloning,
            instruct_voice_design=declared.instruct_voice_design,
            emotions=declared.emotions,
            streaming=declared.streaming,
            supported_languages=list(declared.supported_languages),
            locations=list(self._spec.locations),
        )

    async def initialize(self) -> None:
        base_url = os.getenv(self._base_url_env, "").strip().rstrip("/")
        if not base_url:
            raise EngineUnavailableError(
                f"{self._base_url_env} no configurada; el engine {self.name} queda desactivado"
            )
        if self._model_env and os.getenv(self._model_env):
            self._model = str(os.getenv(self._model_env)).strip()
        if self._timeout_env and os.getenv(self._timeout_env):
            self._timeout = float(str(os.getenv(self._timeout_env)))
        self._client = httpx.AsyncClient(base_url=base_url, timeout=self._timeout)
        self._available = True
        logger.info("OpenAI-compatible engine %s initialized (model=%s)", self.name, self._model)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._available = False

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None or not self._available:
            raise EngineUnavailableError(
                f"Engine {self.name} no disponible; configura {self._base_url_env}"
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        api_key = os.getenv(self._api_key_env, "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
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
                f"{self.name} rechazó /audio/speech: "
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
        configured = bool(os.getenv(self._base_url_env, "").strip())
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
