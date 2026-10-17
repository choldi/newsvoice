"""Multi-engine router — dispatches synthesis requests to the right engine by language."""

from __future__ import annotations

import logging
from typing import Any

from tts_api.core.engine_base import EngineCapabilities, TtsEngineBase
from tts_api.core.exceptions import (
    EngineUnavailableError,
)

logger = logging.getLogger(__name__)


class RoutedEngine(TtsEngineBase):
    """Wraps multiple TTS engines and routes requests by language.

    Language routing is configured via a dict mapping language codes to engine names:
        {"es": "pocket_tts", "en": "pocket_tts", "ca": "edgetts", "_default": "pocket_tts"}

    The ``_default`` key is used when a language has no explicit mapping.
    """

    def __init__(self, engines: dict[str, TtsEngineBase], routing: dict[str, str]) -> None:
        self._engines = engines  # engine_name -> engine instance
        self._routing = routing  # language_code -> engine_name
        self._default_engine_name = routing.get("_default", next(iter(engines)))

    @property
    def name(self) -> str:
        engine_names = ", ".join(self._engines.keys())
        return f"routed({engine_names})"

    @property
    def capabilities(self) -> EngineCapabilities:
        """Union of all sub-engine capabilities."""
        langs: set[str] = set()
        cloning = False
        instruct = False
        emotions = False
        streaming = False
        for engine in self._engines.values():
            cap = engine.capabilities
            langs.update(cap.supported_languages)
            cloning = cloning or cap.voice_cloning
            instruct = instruct or cap.instruct_voice_design
            emotions = emotions or cap.emotions
            streaming = streaming or cap.streaming

        return EngineCapabilities(
            stock_voices=True,
            voice_cloning=cloning,
            instruct_voice_design=instruct,
            emotions=emotions,
            streaming=streaming,
            supported_languages=sorted(langs),
        )

    def _resolve_engine(self, language: str) -> TtsEngineBase:
        """Resolve which engine to use for a given language."""
        engine_name = self._routing.get(language, self._default_engine_name)
        engine = self._engines.get(engine_name)
        if engine is None:
            raise EngineUnavailableError(
                f"Engine '{engine_name}' no está disponible para el idioma '{language}'. "
                f"Engines disponibles: {list(self._engines.keys())}"
            )
        return engine

    async def initialize(self) -> None:
        for name, engine in self._engines.items():
            try:
                await engine.initialize()
                logger.info("RoutedEngine: sub-engine '%s' initialized", name)
            except Exception as e:
                logger.error("RoutedEngine: failed to initialize '%s': %s", name, e)
                raise

    async def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
    ) -> bytes:
        engine = self._resolve_engine(language)
        return await engine.synthesize(
            text=text,
            voice_id=voice_id,
            language=language,
            speed=speed,
            emotion=emotion,
        )

    async def synthesize_clone(
        self,
        *,
        text: str,
        reference_audio_path: str,
        language: str,
        speed: float = 1.0,
        ref_text: str | None = None,
    ) -> bytes:
        engine = self._resolve_engine(language)
        return await engine.synthesize_clone(
            text=text,
            reference_audio_path=reference_audio_path,
            language=language,
            speed=speed,
            ref_text=ref_text,
        )

    async def synthesize_instruct(
        self,
        *,
        text: str,
        instruct: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
    ) -> bytes:
        engine = self._resolve_engine(language)
        return await engine.synthesize_instruct(
            text=text,
            instruct=instruct,
            language=language,
            speed=speed,
            emotion=emotion,
        )

    async def list_stock_voices(self, language: str | None = None) -> list[dict]:
        """List stock voices from all sub-engines (deduplicated by voice_id)."""
        seen: set[str] = set()
        all_voices: list[dict] = []
        for engine in self._engines.values():
            voices = await engine.list_stock_voices(language)
            for v in voices:
                if v["voice_id"] not in seen:
                    seen.add(v["voice_id"])
                    all_voices.append(v)
        return all_voices

    async def health_check(self) -> dict:
        sub_health: dict[str, Any] = {}
        for name, engine in self._engines.items():
            try:
                sub_health[name] = await engine.health_check()
            except Exception as e:
                sub_health[name] = {"error": str(e)}

        return {
            "model_loaded": all(h.get("model_loaded", False) for h in sub_health.values()),
            "gpu_available": any(h.get("gpu_available", False) for h in sub_health.values()),
            "device": "routed",
            "stock_voices_count": sum(h.get("stock_voices_count", 0) for h in sub_health.values()),
            "vram_free_mb": sum(h.get("vram_free_mb", 0) for h in sub_health.values()),
            "mode": "ROUTED",
            "engine": self.name,
            "routing": self._routing,
            "sub_engines": sub_health,
        }

    async def warmup(self) -> None:
        for engine in self._engines.values():
            try:
                await engine.warmup()
            except Exception as e:
                logger.warning("RoutedEngine: warmup failed for '%s': %s", engine.name, e)

    async def close(self) -> None:
        for engine in self._engines.values():
            try:
                await engine.close()
            except Exception as e:
                logger.warning("RoutedEngine: close failed for '%s': %s", engine.name, e)
