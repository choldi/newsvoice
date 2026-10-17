"""Chatterbox Multilingual TTS engine (GPU) — local neural text-to-speech.

Implements the TTS engine contract for the ``chatterbox`` backend using
``chatterbox-tts`` (resemble-ai), the Chatterbox Multilingual (MTL) model.

Capabilities: stock synthesis (23 languages), zero-shot voice cloning from a
reference audio, and instruct-style design mapped to ``exaggeration`` /
``cfg_weight``. No Catalan support.

The model loads lazily on first synthesis so that a cold API start does not
occupy GPU memory until it is actually needed.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import threading
import wave
from pathlib import Path
from typing import Any

from tts_api.core import ffmpeg
from tts_api.core.engine_base import EngineCapabilities, TtsEngineBase
from tts_api.core.exceptions import (
    EngineUnavailableError,
    InvalidReferenceAudioError,
    UnsupportedLanguageError,
    VoiceNotFoundError,
)
from tts_api.settings import get_settings

logger = logging.getLogger(__name__)

# Languages supported by Chatterbox Multilingual (no Catalan).
_SUPPORTED_LANGUAGES = [
    "ar",
    "da",
    "de",
    "el",
    "en",
    "es",
    "fi",
    "fr",
    "he",
    "hi",
    "it",
    "ja",
    "ko",
    "ms",
    "nl",
    "no",
    "pl",
    "pt",
    "ru",
    "sv",
    "sw",
    "tr",
    "zh",
]

# The Multilingual model ships one builtin voice, loaded from `conds.pt`.
_DEFAULT_VOICE_ID = "chatterbox-default"

# Parse "exaggeration=0.7 cfg=0.3" / "cfg_weight=0.4" / "exaggeration=-1".
_INSTRUCT_RE = re.compile(r"(exaggeration|cfg|cfg_weight)\s*[=:]\s*(-?[0-9.]+)")

_EXAGGERATION_RANGE = (0.0, 2.0)
_CFG_RANGE = (0.0, 1.0)
_SPEED_EPSILON = 1e-3


class ChatterboxEngine(TtsEngineBase):
    """Chatterbox Multilingual text-to-speech engine.

    Runs on torch (GPU preferred). The checkpoint is loaded on first use and
    cached; generation is serialised behind a lock because the model is not
    reentrant.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._t3_model: str = settings.CHATTERBOX_T3_MODEL
        self._device_setting: str = settings.CHATTERBOX_DEVICE
        self._exaggeration: float = settings.CHATTERBOX_EXAGGERATION
        self._cfg_weight: float = settings.CHATTERBOX_CFG_WEIGHT
        self._model: Any = None
        self._device: str = self._device_setting
        self._ready = False
        self._load_lock = threading.Lock()
        self._generate_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "chatterbox"

    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            stock_voices=True,
            voice_cloning=True,
            instruct_voice_design=True,
            emotions=False,
            streaming=False,
            supported_languages=list(_SUPPORTED_LANGUAGES),
        )

    async def initialize(self) -> None:
        if self._ready:
            return
        try:
            import chatterbox.mtl_tts  # noqa: F401
        except ImportError as exc:
            raise EngineUnavailableError(
                "chatterbox-tts no está instalado. Ejecuta: pip install chatterbox-tts"
            ) from exc

        self._ready = True
        logger.info(
            "chatterbox engine ready (device=%s, t3_model=%s)",
            self._device_setting,
            self._t3_model,
        )

    async def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
    ) -> bytes:
        self._validate_voice(voice_id)
        self._validate_language(language)

        await self._ensure_model()
        wav = await asyncio.to_thread(self._generate, text, language, None)
        return self._apply_speed(self._tensor_to_wav(wav), speed)

    async def synthesize_clone(
        self,
        *,
        text: str,
        reference_audio_path: str,
        language: str,
        speed: float = 1.0,
        ref_text: str | None = None,
    ) -> bytes:
        self._validate_language(language)
        if not reference_audio_path or not Path(reference_audio_path).exists():
            raise InvalidReferenceAudioError(f"Audio de referencia ausente: {reference_audio_path}")

        await self._ensure_model()
        wav = await asyncio.to_thread(self._generate, text, language, reference_audio_path)
        return self._apply_speed(self._tensor_to_wav(wav), speed)

    async def synthesize_instruct(
        self,
        *,
        text: str,
        instruct: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
    ) -> bytes:
        self._validate_language(language)
        exaggeration, cfg_weight = self._parse_instruct(instruct)

        await self._ensure_model()
        wav = await asyncio.to_thread(
            self._generate, text, language, None, exaggeration, cfg_weight
        )
        return self._apply_speed(self._tensor_to_wav(wav), speed)

    async def list_stock_voices(self, language: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                "voice_id": _DEFAULT_VOICE_ID,
                "language": language or "und",
                "gender": "neutral",
                "name": "Chatterbox default voice",
            }
        ]

    async def health_check(self) -> dict[str, Any]:
        loaded = self._model is not None
        return {
            "model_loaded": loaded,
            "device": self._device,
            "gpu_available": self._device.startswith("cuda"),
            "stock_voices_count": 1,
            "mode": "REAL" if loaded else "NOT_LOADED",
            "engine": self.name,
        }

    async def close(self) -> None:
        self._model = None
        self._ready = False

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _validate_voice(voice_id: str) -> None:
        if voice_id not in ("", _DEFAULT_VOICE_ID):
            err = VoiceNotFoundError(voice_id, "stock")
            err.valid_voice_ids = [_DEFAULT_VOICE_ID]
            raise err

    @staticmethod
    def _validate_language(language: str) -> None:
        if language not in _SUPPORTED_LANGUAGES:
            raise UnsupportedLanguageError(language, list(_SUPPORTED_LANGUAGES))

    @staticmethod
    def _parse_instruct(instruct: str) -> tuple[float, float]:
        """Map free-form instruct text onto (exaggeration, cfg_weight)."""
        exaggeration: float | None = None
        cfg_weight: float | None = None
        for match in _INSTRUCT_RE.finditer(instruct):
            key, raw = match.group(1), match.group(2)
            try:
                value = float(raw)
            except ValueError:
                continue
            if key == "exaggeration":
                exaggeration = value
            else:
                cfg_weight = value
        ex = exaggeration if exaggeration is not None else 0.5
        cfg = cfg_weight if cfg_weight is not None else 0.5
        lo, hi = _EXAGGERATION_RANGE
        ex = max(lo, min(hi, ex))
        lo, hi = _CFG_RANGE
        cfg = max(lo, min(hi, cfg))
        return ex, cfg

    def _resolve_device(self) -> str:
        import torch

        device = self._device_setting
        if device.startswith("cuda") and not torch.cuda.is_available():
            logger.warning("CUDA no disponible; chatterbox usará CPU")
            return "cpu"
        return device

    def _load_model(self) -> Any:
        with self._load_lock:
            if self._model is not None:
                return self._model
            try:
                from chatterbox.mtl_tts import ChatterboxMultilingualTTS
            except ImportError as exc:
                raise EngineUnavailableError(
                    "chatterbox-tts no está instalado. Ejecuta: pip install chatterbox-tts"
                ) from exc

            self._device = self._resolve_device()
            try:
                self._model = ChatterboxMultilingualTTS.from_pretrained(
                    device=self._device, t3_model=self._t3_model
                )
            except Exception as exc:
                self._device = "cpu"
                self._model = ChatterboxMultilingualTTS.from_pretrained(
                    device="cpu", t3_model=self._t3_model
                )
                logger.warning("chatterbox GPU load failed; fell back to CPU: %s", exc)
            logger.info(
                "chatterbox model loaded (device=%s, t3_model=%s)", self._device, self._t3_model
            )
            return self._model

    async def _ensure_model(self) -> Any:
        if self._model is None:
            return await asyncio.to_thread(self._load_model)
        return self._model

    def _generate(
        self,
        text: str,
        language: str,
        audio_prompt_path: str | None,
        exaggeration: float | None = None,
        cfg_weight: float | None = None,
    ) -> Any:
        model = self._model
        kwargs: dict[str, Any] = {"language_id": language}
        if audio_prompt_path:
            kwargs["audio_prompt_path"] = audio_prompt_path
        if exaggeration is not None:
            kwargs["exaggeration"] = exaggeration
        if cfg_weight is not None:
            kwargs["cfg_weight"] = cfg_weight
        with self._generate_lock:
            return model.generate(text, **kwargs)

    def _apply_speed(self, wav_bytes: bytes, speed: float) -> bytes:
        """Ajusta la velocidad conservando el tono (ffmpeg ``atempo``).

        Chatterbox no admite ``speed`` nativo, así que se hace un
        time-stretch posterior. Si ``ffmpeg`` no está disponible se
        devuelve el audio original (sin fallar la petición).

        Args:
            wav_bytes: Audio WAV mono PCM de entrada.
            speed: Factor de velocidad (>1 acelera, <1 ralentiza).

        Returns:
            El WAV reescrito, o el original si no procede estirar.
        """
        if speed <= 0 or abs(speed - 1.0) < _SPEED_EPSILON:
            return wav_bytes

        import soundfile as sf

        audio, sample_rate = sf.read(io.BytesIO(wav_bytes), dtype="float32")
        stretched = ffmpeg.time_stretch(audio, sample_rate, speed)
        if stretched is None:
            logger.warning("speed=%s ignorado: falta el binario 'ffmpeg'", speed)
            return wav_bytes
        buffer = io.BytesIO()
        sf.write(buffer, stretched, sample_rate, format="WAV", subtype="PCM_16")
        return buffer.getvalue()

    def _tensor_to_wav(self, wav: Any) -> bytes:
        import numpy as np

        sample_rate = int(getattr(self._model, "sr", 44100))
        if hasattr(wav, "detach"):
            wav = wav.detach().cpu().numpy()
        audio = np.asarray(wav, dtype=np.float32).squeeze()
        pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(sample_rate)
            out.writeframes(pcm.tobytes())
        return buffer.getvalue()
