"""Pocket TTS engine (Kyutai) — CPU-first with voice cloning."""

from __future__ import annotations

import asyncio
import io
import logging
import threading
import wave
from collections import OrderedDict
from pathlib import Path
from typing import Any

from tts_api.core import ffmpeg
from tts_api.core.engine_base import EngineCapabilities, TtsEngineBase
from tts_api.core.exceptions import (
    EngineUnavailableError,
    FeatureNotSupportedError,
    UnsupportedLanguageError,
    VoiceNotFoundError,
)
from tts_api.settings import get_settings

logger = logging.getLogger(__name__)

# Estados de clonado retenidos en memoria (1 por voz en uso). Un estado pesa
# ~1 MB por segundo de referencia; 4 voces mantienen el render de un podcast
# entero sin recalcular el prompt y sin disparar el uso de RAM.
_MAX_CACHED_CLONE_STATES = 4

# Tolerancia para tratar `speed` como 1.0 (evita time-stretch inútil).
_SPEED_EPSILON = 1e-3


def _stretch_audio(audio: Any, sample_rate: int, speed: float) -> Any:
    """Acelera ``audio`` con el time-stretch de ``ffmpeg`` (conserva el tono).

    Args:
        audio: Señal mono de muestras float32.
        sample_rate: Frecuencia de muestreo en Hz.
        speed: Factor de velocidad.

    Returns:
        La señal estirada en el mismo formato de entrada.

    Raises:
        EngineUnavailableError: Si ``ffmpeg`` no está disponible.
    """
    stretched = ffmpeg.time_stretch(audio, sample_rate, speed)
    if stretched is None:
        raise EngineUnavailableError(
            "No se puede aplicar 'speed': hace falta el binario 'ffmpeg' "
            "(librosa no se usa, ver README.md)"
        )
    return stretched


# Pocket TTS preset voice names
_POCKET_VOICE_PRESETS: dict[str, str] = {
    "alba": "alba",
    "anna": "anna",
    "giovanni": "giovanni",
    "lola": "lola",
}

# Mapping from our voice_id scheme to Pocket TTS voice presets
_STOCK_VOICE_MAP: dict[str, dict[str, str]] = {
    "es-mx-male": {
        "preset": "giovanni",
        "language": "es",
        "gender": "male",
        "name": "Spanish MX Male",
    },
    "es-mx-female": {
        "preset": "lola",
        "language": "es",
        "gender": "female",
        "name": "Spanish MX Female",
    },
    "es-es-male": {
        "preset": "giovanni",
        "language": "es",
        "gender": "male",
        "name": "Spanish Spain Male",
    },
    "es-es-female": {
        "preset": "lola",
        "language": "es",
        "gender": "female",
        "name": "Spanish Spain Female",
    },
    "en-us-male": {
        "preset": "giovanni",
        "language": "en",
        "gender": "male",
        "name": "English US Male",
    },
    "en-us-female": {
        "preset": "anna",
        "language": "en",
        "gender": "female",
        "name": "English US Female",
    },
    "en-gb-male": {
        "preset": "giovanni",
        "language": "en",
        "gender": "male",
        "name": "English UK Male",
    },
    "en-gb-female": {
        "preset": "alba",
        "language": "en",
        "gender": "female",
        "name": "English UK Female",
    },
    "fr-fr-male": {"preset": "giovanni", "language": "fr", "gender": "male", "name": "French Male"},
    "fr-fr-female": {
        "preset": "alba",
        "language": "fr",
        "gender": "female",
        "name": "French Female",
    },
}

# Languages Pocket TTS supports
_POCKET_LANGUAGES = {"en", "es", "fr", "de", "it", "pt"}


class PocketTTSEngine(TtsEngineBase):
    """Pocket TTS engine — CPU-first, supports voice cloning from audio prompts."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._model: Any = None
        self._voice_states: dict[str, Any] = {}
        self._clone_states: OrderedDict[tuple[str, float, int], Any] = OrderedDict()
        # pocket-tts documenta generate_audio() y get_state_for_audio_prompt()
        # como NO thread-safe: generación y cálculo de prompt se serializan
        # con un lock de thread (el trabajo corre en asyncio.to_thread, así
        # que el event loop nunca queda bloqueado esperándolo).
        self._generate_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "pocket_tts"

    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            stock_voices=True,
            voice_cloning=True,
            instruct_voice_design=False,
            emotions=False,
            streaming=False,
            supported_languages=list(_POCKET_LANGUAGES),
        )

    def _model_kwargs(self) -> dict[str, Any]:
        """Traduce ``POCKET_TTS_MODEL`` a argumentos válidos de ``load_model``.

        Acepta un código de idioma (``english``) como ``language=`` o una
        ruta/URL de config YAML (``hf://repo/config.yaml``) como ``config=``.

        Returns:
            Argumentos de keyword para ``TTSModel.load_model``; vacío si el
            valor no es utilizable y procede usar la config por defecto.
        """
        value = (self._settings.POCKET_TTS_MODEL or "").strip()
        if not value:
            return {}
        if value.startswith(("hf://", "http://", "https://")) or value.endswith((".yaml", ".yml")):
            return {"config": value}
        if Path(value).exists():
            return {"config": value}
        if "/" not in value and "\\" not in value:
            return {"language": value}
        logger.warning(
            "POCKET_TTS_MODEL=%r no es ni un codigo de idioma ni un fichero .yaml: "
            "se ignora y se usa la config por defecto.",
            value,
        )
        return {}

    async def initialize(self) -> None:
        if self._model is not None:
            return
        try:
            from pocket_tts import TTSModel

            kwargs = self._model_kwargs()
            logger.info(
                "Initializing Pocket TTS engine (model=%s, load=%s)...",
                self._settings.POCKET_TTS_MODEL,
                kwargs or "defaults (english)",
            )
            self._model = await asyncio.to_thread(TTSModel.load_model, **kwargs)
            # Pre-load voice states for stock presets
            for preset_name in _POCKET_VOICE_PRESETS:
                try:
                    state = await asyncio.to_thread(
                        self._model.get_state_for_audio_prompt, preset_name
                    )
                    self._voice_states[preset_name] = state
                except Exception as e:
                    logger.warning("Could not load voice preset '%s': %s", preset_name, e)
            logger.info("Pocket TTS engine initialized")
        except ImportError:
            raise EngineUnavailableError(
                "pocket-tts no está instalado. Ejecuta: pip install pocket-tts"
            ) from None
        except Exception as e:
            raise EngineUnavailableError(f"Error inicializando Pocket TTS: {e}") from e

    async def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
    ) -> bytes:
        voice = _STOCK_VOICE_MAP.get(voice_id)
        if not voice:
            valid_ids = list(_STOCK_VOICE_MAP.keys())
            err = VoiceNotFoundError(voice_id, "stock")
            err.valid_voice_ids = valid_ids
            raise err

        if language not in _POCKET_LANGUAGES:
            raise UnsupportedLanguageError(language, list(_POCKET_LANGUAGES))

        preset = voice["preset"]
        state = self._voice_states.get(preset)
        if state is None:
            raise EngineUnavailableError(f"Voice preset '{preset}' no cargado")

        try:
            return await asyncio.to_thread(self._render_locked, state, text, speed)
        except Exception as e:
            raise EngineUnavailableError(f"Error sintetizando con Pocket TTS: {e}") from e

    async def synthesize_clone(
        self,
        *,
        text: str,
        reference_audio_path: str,
        language: str,
        speed: float = 1.0,
        ref_text: str | None = None,
    ) -> bytes:
        if not Path(reference_audio_path).exists():
            raise EngineUnavailableError(f"Reference audio no encontrado: {reference_audio_path}")

        try:
            return await asyncio.to_thread(
                self._render_clone_locked, reference_audio_path, text, speed
            )
        except Exception as e:
            raise EngineUnavailableError(f"Error clonando voz con Pocket TTS: {e}") from e

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

    async def list_stock_voices(self, language: str | None = None) -> list[dict]:
        voices = [
            {"voice_id": vid, "language": v["language"], "gender": v["gender"], "name": v["name"]}
            for vid, v in _STOCK_VOICE_MAP.items()
        ]
        if language:
            voices = [v for v in voices if v["language"] == language]
        return voices

    async def health_check(self) -> dict:
        return {
            "model_loaded": self._model is not None,
            "gpu_available": False,
            "device": "cpu",
            "stock_voices_count": len(_STOCK_VOICE_MAP),
            "vram_free_mb": 0,
            "mode": "REAL" if self._model else "NOT_LOADED",
            "engine": self.name,
        }

    def _render_locked(self, state: Any, text: str, speed: float) -> bytes:
        """Genera audio de voz stock serializando el acceso al modelo."""
        with self._generate_lock:
            audio = self._model.generate_audio(state, text)
        return self._apply_speed(self._tensor_to_wav(audio), speed)

    def _render_clone_locked(self, reference_audio_path: str, text: str, speed: float) -> bytes:
        """Genera audio clonado serializando cálculo de prompt + generación."""
        with self._generate_lock:
            state = self._clone_state(reference_audio_path)
            audio = self._model.generate_audio(state, text)
        return self._apply_speed(self._tensor_to_wav(audio), speed)

    def _clone_state(self, reference_audio_path: str) -> Any:
        """Estado de clonado cacheado por fichero de referencia (bajo el lock).

        Recalcular el estado en cada segmento cuesta ~0.7 s, por lo que un
        render de 2 voces lo hacía unas 200 veces. La clave incluye tamaño y
        mtime para invalidarse si cambia la referencia.
        """
        path = Path(reference_audio_path)
        stat = path.stat()
        key = (str(path), stat.st_mtime, stat.st_size)
        cached = self._clone_states.get(key)
        if cached is not None:
            self._clone_states.move_to_end(key)
            return cached
        state = self._model.get_state_for_audio_prompt(reference_audio_path)
        self._clone_states[key] = state
        while len(self._clone_states) > _MAX_CACHED_CLONE_STATES:
            self._clone_states.popitem(last=False)
        return state

    def _apply_speed(self, wav_bytes: bytes, speed: float) -> bytes:
        """Ajusta la velocidad del audio conservando el tono.

        Pocket TTS no admite velocidad nativa, así que el cambio se hace con
        un time-stretch posterior de ``ffmpeg`` (``atempo``). Un ``speed`` nulo
        o muy próximo a 1.0 se ignora para no pagar el coste del estiramiento.

        Args:
            wav_bytes: Audio WAV mono PCM de entrada.
            speed: Factor de velocidad (>1 acelera, <1 ralentiza).

        Returns:
            El WAV reescrito, o el original si ``speed`` equivale a 1.0.

        Raises:
            EngineUnavailableError: Si ``ffmpeg`` no está disponible.
        """
        if speed <= 0 or abs(speed - 1.0) < _SPEED_EPSILON:
            return wav_bytes

        import soundfile as sf

        audio, sample_rate = sf.read(io.BytesIO(wav_bytes), dtype="float32")
        stretched = _stretch_audio(audio, sample_rate, speed)
        buffer = io.BytesIO()
        sf.write(buffer, stretched, sample_rate, format="WAV", subtype="PCM_16")
        return buffer.getvalue()

    def _tensor_to_wav(self, audio: Any) -> bytes:
        """Convert tensor/numpy output from Pocket TTS to WAV bytes."""
        import numpy as np

        if hasattr(audio, "numpy"):
            audio = audio.numpy()
        audio = np.asarray(audio, dtype=np.float32)

        if audio.ndim > 1:
            audio = audio.flatten()

        sample_rate = getattr(self._model, "sample_rate", 24000)
        audio_int16 = (audio * 32767).clip(-32768, 32767).astype(np.int16)

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio_int16.tobytes())
        return buffer.getvalue()
