"""HTTP proxy engine for the isolated OmniVoice sidecar.

The real OmniVoice package pins ``librosa`` (numba) and ``transformers>=5.3``,
which cannot coexist with ``pocket_tts`` (``numpy>=2``) in the same virtualenv.
Instead of importing it here, a dedicated ``gateway-omnivoice`` container runs
the same ``tts_api`` app with the real engine, and this class forwards requests
over HTTP. The two processes therefore share no Python environment.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from tts_api.core.engine_base import EngineCapabilities, TtsEngineBase
from tts_api.core.exceptions import EngineUnavailableError
from tts_api.settings import get_settings

logger = logging.getLogger(__name__)


class OmniVoiceRemoteEngine(TtsEngineBase):
    """Proxy that forwards synthesis to a remote OmniVoice ``tts_api`` instance."""

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        settings = get_settings()
        self._base_url = (base_url or settings.OMNIVOICE_REMOTE_URL).strip().rstrip("/")
        self._timeout = timeout or settings.OMNIVOICE_REMOTE_TIMEOUT
        self._client: httpx.AsyncClient | None = None
        self._available = False

    @property
    def name(self) -> str:
        return "omnivoice_remote"

    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            stock_voices=True,
            voice_cloning=True,
            instruct_voice_design=True,
            emotions=True,
            streaming=False,
            supported_languages=["es", "en", "fr", "de", "it", "pt", "zh", "ja", "ko"],
            locations=["local"],
        )

    # --- lifecycle ---------------------------------------------------------

    async def initialize(self) -> None:
        """Create the HTTP client and probe the sidecar.

        An unreachable sidecar is non-fatal: the engine stays unavailable for
        synthesis but does not prevent the main gateway (or a routed engine
        that includes it) from starting.
        """
        if not self._base_url:
            logger.warning(
                "OMNIVOICE_REMOTE_URL no configurada; el engine omnivoice_remote "
                "quedará no disponible"
            )
            return

        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)
        try:
            response = await self._client.get("/api/v1/health/live")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning(
                "Sidecar OmniVoice no disponible en %s: %s; el engine "
                "omnivoice_remote quedará no disponible",
                self._base_url,
                exc,
            )
            return
        self._available = True

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._available = False

    # --- helpers -----------------------------------------------------------

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None or not self._available:
            raise EngineUnavailableError(
                "Sidecar OmniVoice no disponible; configura OMNIVOICE_REMOTE_URL "
                "y asegúrate de que el servicio gateway-omnivoice está arriba"
            )
        return self._client

    async def _post_audio(self, path: str, payload: dict[str, Any]) -> bytes:
        client = self._require_client()
        try:
            response = await client.post(path, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise EngineUnavailableError(
                f"El sidecar OmniVoice rechazó {path}: "
                f"{exc.response.status_code} {exc.response.text[:300]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise EngineUnavailableError(f"Error llamando al sidecar OmniVoice: {exc}") from exc
        return response.content

    # --- synthesis ---------------------------------------------------------

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
        payload: dict[str, Any] = {
            "text": text,
            "voice_id": voice_id,
            "language": language,
            "speed": speed,
        }
        if emotion:
            payload["emotion"] = emotion
        return await self._post_audio("/api/v1/tts", payload)

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
        payload: dict[str, Any] = {
            "text": text,
            "reference_audio_path": reference_audio_path,
            "language": language,
            "speed": speed,
        }
        if ref_text:
            payload["ref_text"] = ref_text
        return await self._post_audio("/api/v1/tts/clone", payload)

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
        payload: dict[str, Any] = {
            "text": text,
            "instruct": instruct,
            "language": language,
            "speed": speed,
        }
        if emotion:
            payload["emotion"] = emotion
        return await self._post_audio("/api/v1/tts/instruct", payload)

    # --- introspection -----------------------------------------------------

    async def list_stock_voices(self, language: str | None = None) -> list[dict[str, Any]]:
        client = self._require_client()
        params = {"language": language} if language else None
        try:
            response = await client.get("/api/v1/voices/stock", params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise EngineUnavailableError(f"Error listando voces del sidecar: {exc}") from exc
        return [dict(item) for item in response.json()]

    async def health_check(self) -> dict[str, Any]:
        """Estado del sidecar con ``model_loaded`` presente en todos los casos.

        ``RoutedEngine`` combina sus sub-engines con ``all(...)`` sobre esa
        clave: un sub-engine que no la expone marca el gateway entero como
        ``degraded``, aunque el resto de engines estén sanos.
        """
        if self._client is None or not self._available:
            return {
                "status": "unavailable",
                "engine": self.name,
                "url": self._base_url,
                "model_loaded": False,
            }
        try:
            response = await self._client.get("/api/v1/health")
            response.raise_for_status()
            payload = dict(response.json())
        except httpx.HTTPError as exc:
            return {
                "status": "error",
                "engine": self.name,
                "error": str(exc),
                "model_loaded": False,
            }
        return {
            "status": "ok",
            "engine": self.name,
            "model_loaded": self._remote_model_loaded(payload),
            "remote": payload,
        }

    @staticmethod
    def _remote_model_loaded(payload: dict[str, Any]) -> bool:
        """Deriva el estado del modelo desde la respuesta del sidecar.

        El ``/api/v1/health`` del sidecar no siempre incluye ``model_loaded``
        (el endpoint top-level no lo expone), así que se infiere de
        ``status``/``mode``: un sidecar ``degraded`` o en ``MOCK`` no cuenta
        como modelo real cargado.
        """
        if "model_loaded" in payload:
            return bool(payload["model_loaded"])
        status = payload.get("status")
        if status is None:
            return True
        return status == "ok" and payload.get("mode", "REAL") != "MOCK"
