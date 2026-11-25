"""HTTP proxy for the isolated Chatterbox sidecar.

The real ``chatterbox-tts`` runtime pins ``numpy<2`` and cannot share the main
gateway virtualenv with ``pocket_tts``. The ``gateway-chatterbox`` sidecar runs
the real ``ChatterboxEngine`` in its own image; this proxy keeps the public
engine id as ``chatterbox`` by inheriting the local engine's contract.
"""

from __future__ import annotations

import os
from typing import Any

from tts_api.core.engines.chatterbox_engine import ChatterboxEngine
from tts_api.core.engines.omnivoice_remote_engine import (
    OmniVoiceRemoteEngine,
    normalize_sidecar_base_url,
)

REMOTE_URL_ENV = "CHATTERBOX_REMOTE_URL"
REMOTE_TIMEOUT_ENV = "CHATTERBOX_REMOTE_TIMEOUT"


class ChatterboxRemoteEngine(ChatterboxEngine):
    """Proxy that forwards Chatterbox synthesis to a remote ``tts_api`` instance."""

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        super().__init__()
        configured_url = base_url if base_url is not None else os.getenv(REMOTE_URL_ENV, "")
        configured_timeout = timeout
        if configured_timeout is None:
            raw_timeout = os.getenv(REMOTE_TIMEOUT_ENV, "").strip()
            configured_timeout = float(raw_timeout) if raw_timeout else None
        self._base_url = normalize_sidecar_base_url(configured_url)
        self._proxy = OmniVoiceRemoteEngine(base_url=self._base_url, timeout=configured_timeout)

    async def initialize(self) -> None:
        await self._proxy.initialize()

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
        return await self._proxy.synthesize(
            text=text,
            voice_id=voice_id,
            language=language,
            speed=speed,
            emotion=emotion,
            engine_name=engine_name,
        )

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
        return await self._proxy.synthesize_clone(
            text=text,
            reference_audio_path=reference_audio_path,
            language=language,
            speed=speed,
            ref_text=ref_text,
            engine_name=engine_name,
        )

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
        return await self._proxy.synthesize_instruct(
            text=text,
            instruct=instruct,
            language=language,
            speed=speed,
            emotion=emotion,
            engine_name=engine_name,
        )

    async def list_stock_voices(self, language: str | None = None) -> list[dict[str, Any]]:
        return await self._proxy.list_stock_voices(language)

    async def health_check(self) -> dict[str, Any]:
        health = await self._proxy.health_check()
        health["engine"] = self.name
        return health

    async def close(self) -> None:
        await self._proxy.close()
        await super().close()
