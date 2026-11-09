"""HTTP proxy for the isolated Qwen3-TTS sidecar.

The Qwen3 runtime pins ``transformers==4.57.3``/``accelerate==1.12.0``, which
cannot share a virtualenv with the gateway's pocket_tts/transformers stack. The
``gateway-qwen3`` sidecar runs the same ``tts_api`` app with ``TTS_ENGINE``
``qwen3_tts``; this proxy keeps the public engine name as ``qwen3_tts`` so the
news-engine's per-voice pin continues to work unchanged.
"""

from __future__ import annotations

from tts_api.core.engine_base import EngineCapabilities
from tts_api.core.engines.omnivoice_remote_engine import OmniVoiceRemoteEngine
from tts_api.settings import get_settings


class Qwen3RemoteEngine(OmniVoiceRemoteEngine):
    """Proxy that forwards synthesis to a remote Qwen3-TTS ``tts_api`` instance."""

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        settings = get_settings()
        super().__init__(
            base_url=base_url or settings.QWEN3_TTS_REMOTE_URL,
            timeout=timeout or settings.QWEN3_TTS_REMOTE_TIMEOUT,
        )

    @property
    def name(self) -> str:
        return "qwen3_tts"

    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            stock_voices=True,
            voice_cloning=True,
            instruct_voice_design=True,
            emotions=False,
            streaming=False,
            supported_languages=["es", "en", "fr", "de", "it", "pt", "zh", "ja", "ko", "ca", "ru"],
            locations=["local"],
        )
