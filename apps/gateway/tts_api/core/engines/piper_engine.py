"""Piper TTS engine (ONNX, CPU-first) — local neural text-to-speech.

Implements the TTS engine contract for the ``piper`` backend using
``piper-tts`` (OHF-Voice/piper1-gpl) and ONNX voices from
``rhasspy/piper-voices``.

Capabilities: stock voice synthesis (ca/es/en/fr), speed control, voice
download. No cloning, no instruct voice design, no emotion control.
"""

from __future__ import annotations

import asyncio
import io
import logging
import wave
from pathlib import Path
from typing import Any

from tts_api.core.engine_base import EngineCapabilities, TtsEngineBase
from tts_api.core.exceptions import (
    EngineUnavailableError,
    FeatureNotSupportedError,
    UnsupportedLanguageError,
    VoiceNotFoundError,
)
from tts_api.settings import get_settings

logger = logging.getLogger(__name__)

# Gateway voice_id -> Piper voice model (name.quality) + metadata.
# Model names verified against huggingface.co/rhasspy/piper-voices.
_STOCK_VOICE_MAP: dict[str, dict[str, str]] = {
    "ca-female": {
        "model": "ca_ES-upc_ona-medium",
        "language": "ca",
        "gender": "female",
        "name": "Catalan Female (Ona)",
    },
    "ca-male": {
        "model": "ca_ES-upc_pau-x_low",
        "language": "ca",
        "gender": "male",
        "name": "Catalan Male (Pau)",
    },
    "es-es-female": {
        "model": "es_ES-sharvard-medium",
        "language": "es",
        "gender": "female",
        "name": "Spanish Female (ES, Sharvard)",
    },
    "es-es-male": {
        "model": "es_ES-davefx-medium",
        "language": "es",
        "gender": "male",
        "name": "Spanish Male (ES, DaveFX)",
    },
    "es-mx-female": {
        "model": "es_MX-claude-high",
        "language": "es",
        "gender": "female",
        "name": "Spanish Female (MX, Claude)",
    },
    "es-mx-male": {
        "model": "es_MX-ald-medium",
        "language": "es",
        "gender": "male",
        "name": "Spanish Male (MX, Ald)",
    },
    "en-us-female": {
        "model": "en_US-amy-medium",
        "language": "en",
        "gender": "female",
        "name": "English Female (US, Amy)",
    },
    "en-us-male": {
        "model": "en_US-ryan-medium",
        "language": "en",
        "gender": "male",
        "name": "English Male (US, Ryan)",
    },
    "en-gb-female": {
        "model": "en_GB-alba-medium",
        "language": "en",
        "gender": "female",
        "name": "English Female (GB, Alba)",
    },
    "en-gb-male": {
        "model": "en_GB-alan-medium",
        "language": "en",
        "gender": "male",
        "name": "English Male (GB, Alan)",
    },
    "fr-fr-female": {
        "model": "fr_FR-siwis-medium",
        "language": "fr",
        "gender": "female",
        "name": "French Female (Siwis)",
    },
    "fr-fr-male": {
        "model": "fr_FR-tom-medium",
        "language": "fr",
        "gender": "male",
        "name": "French Male (Tom)",
    },
}

_SUPPORTED_LANGUAGES = sorted({meta["language"] for meta in _STOCK_VOICE_MAP.values()})

# Piper length_scale = 1 / speed. Clamp to keep synthesis sane.
_MIN_LENGTH_SCALE = 0.25
_MAX_LENGTH_SCALE = 4.0


class PiperEngine(TtsEngineBase):
    """Piper ONNX text-to-speech engine (CPU-first).

    Synthesis uses :class:`piper.PiperVoice` on top of ONNX Runtime. Voices
    load lazily and are cached per model name.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._voices_dir: Path = settings.PIPER_VOICES_DIR
        self._default_voice: str = settings.PIPER_VOICE
        self._use_cuda: bool = settings.PIPER_USE_CUDA
        self._auto_download: bool = settings.PIPER_AUTO_DOWNLOAD
        self._voices: dict[str, Any] = {}
        self._ready = False

    @property
    def name(self) -> str:
        return "piper"

    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            stock_voices=True,
            voice_cloning=False,
            instruct_voice_design=False,
            emotions=False,
            streaming=False,
            supported_languages=list(_SUPPORTED_LANGUAGES),
        )

    async def initialize(self) -> None:
        if self._ready:
            return
        try:
            import piper  # noqa: F401
        except ImportError as exc:
            raise EngineUnavailableError(
                "piper-tts no está instalado. Ejecuta: pip install piper-tts"
            ) from exc

        self._ready = True
        if self._default_voice:
            try:
                await asyncio.to_thread(self._load_voice, self._default_voice)
                logger.info("piper default voice loaded: %s", self._default_voice)
            except EngineUnavailableError as exc:
                logger.warning(
                    "piper default voice not preloaded (%s): %s", self._default_voice, exc
                )
        logger.info("piper engine ready (device=%s)", self._device())

    async def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
    ) -> bytes:
        meta = _STOCK_VOICE_MAP.get(voice_id)
        if meta is None:
            err = VoiceNotFoundError(voice_id, "stock")
            err.valid_voice_ids = list(_STOCK_VOICE_MAP)
            raise err
        if language not in _SUPPORTED_LANGUAGES:
            raise UnsupportedLanguageError(language, list(_SUPPORTED_LANGUAGES))

        voice = await asyncio.to_thread(self._load_voice, meta["model"])
        return await asyncio.to_thread(self._render, voice, text, speed)

    async def synthesize_clone(
        self,
        *,
        text: str,
        reference_audio_path: str,
        language: str,
        speed: float = 1.0,
        ref_text: str | None = None,
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
    ) -> bytes:
        raise FeatureNotSupportedError("instruct/voice design", self.name)

    async def list_stock_voices(self, language: str | None = None) -> list[dict[str, Any]]:
        voices = []
        for voice_id, meta in _STOCK_VOICE_MAP.items():
            if language is not None and meta["language"] != language:
                continue
            voices.append(
                {
                    "voice_id": voice_id,
                    "language": meta["language"],
                    "gender": meta["gender"],
                    "name": meta["name"],
                }
            )
        return voices

    async def health_check(self) -> dict[str, Any]:
        loaded = len(self._voices)
        return {
            "model_loaded": loaded > 0,
            "device": self._device(),
            "gpu_available": self._use_cuda,
            "stock_voices_count": len(_STOCK_VOICE_MAP),
            "mode": "STOCK" if loaded else "NOT_LOADED",
            "engine": self.name,
        }

    async def close(self) -> None:
        self._voices.clear()
        self._ready = False

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _device(self) -> str:
        return "cuda" if self._use_cuda else "cpu"

    def _load_voice(self, model_name: str) -> Any:
        cached = self._voices.get(model_name)
        if cached is not None:
            return cached

        path = self._voices_dir / f"{model_name}.onnx"
        if not path.exists():
            if not self._auto_download:
                raise EngineUnavailableError(
                    f"Modelo Piper ausente: {path}. Usa PIPER_AUTO_DOWNLOAD=true o "
                    f"descárgalo con: python -m piper.download_voices {model_name}"
                )
            self._download_voice(model_name)

        if not path.exists():
            raise EngineUnavailableError(f"Modelo Piper no disponible: {path}")

        from piper import PiperVoice

        config_path = path.with_name(path.name + ".json")
        try:
            voice = PiperVoice.load(
                str(path),
                config_path=str(config_path) if config_path.exists() else None,
                use_cuda=self._use_cuda,
            )
        except Exception as exc:  # pragma: no cover - passthrough load errors
            raise EngineUnavailableError(f"Error cargando modelo Piper {path}: {exc}") from exc

        self._voices[model_name] = voice
        logger.info("piper voice loaded: %s", model_name)
        return voice

    def _download_voice(self, model_name: str) -> None:
        try:
            from piper.download import ensure_voice_exists, get_voices
        except ImportError as exc:  # pragma: no cover - version drift
            raise EngineUnavailableError(
                "piper.download no disponible; actualiza piper-tts."
            ) from exc

        self._voices_dir.mkdir(parents=True, exist_ok=True)
        try:
            voices_info = get_voices(self._voices_dir)
            ensure_voice_exists(model_name, [self._voices_dir], self._voices_dir, voices_info)
        except Exception as exc:
            raise EngineUnavailableError(
                f"No se pudo descargar la voz Piper '{model_name}': {exc}"
            ) from exc

    @staticmethod
    def _length_scale(speed: float) -> float:
        if speed <= 0:
            speed = 1.0
        scale = 1.0 / speed
        return max(_MIN_LENGTH_SCALE, min(_MAX_LENGTH_SCALE, scale))

    def _render(self, voice: Any, text: str, speed: float) -> bytes:
        from piper import SynthesisConfig

        syn = SynthesisConfig(length_scale=self._length_scale(speed))
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            voice.synthesize_wav(text, wav, syn_config=syn)
        return buffer.getvalue()
