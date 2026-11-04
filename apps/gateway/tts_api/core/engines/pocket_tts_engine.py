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


# Mapping from our voice_id scheme to a pocket_tts voice. Each entry declares
# the stock voice's real language (which selects the per-language model) and
# either an upstream ``preset`` (English presets only) or a ``reference`` path
# to a real-language audio prompt used for zero-shot cloning.
#
# ES/FR reuse the SAME voice_id as before, but now render through the matching
# language model with a real ES/FR reference instead of an English preset.
_STOCK_VOICE_MAP: dict[str, dict[str, str]] = {
    "es-mx-male": {
        "reference": "es_male",
        "language": "es",
        "gender": "male",
        "name": "Spanish MX Male",
    },
    "es-mx-female": {
        "reference": "es_female",
        "language": "es",
        "gender": "female",
        "name": "Spanish MX Female",
    },
    "es-es-male": {
        "reference": "es_male",
        "language": "es",
        "gender": "male",
        "name": "Spanish Spain Male",
    },
    "es-es-female": {
        "reference": "es_female",
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
    "fr-fr-male": {
        "reference": "fr_male",
        "language": "fr",
        "gender": "male",
        "name": "French Male",
    },
    "fr-fr-female": {
        "reference": "fr_female",
        "language": "fr",
        "gender": "female",
        "name": "French Female",
    },
}

# Languages Pocket TTS ships a dedicated model for.
_POCKET_LANGUAGES = {"en", "es", "fr", "de", "it", "pt"}

# ISO 639-1 code -> pocket_tts ``load_model(language=...)`` name. Only the
# languages with a dedicated checkpoint are listed; anything else falls back
# to the default language.
_POCKET_LANGUAGE_MODELS: dict[str, str] = {
    "en": "english",
    "es": "spanish",
    "fr": "french",
    "de": "german",
    "it": "italian",
    "pt": "portuguese",
    "nl": "dutch",
}


class PocketTTSEngine(TtsEngineBase):
    """Pocket TTS engine — CPU-first, supports voice cloning from audio prompts.

    Models are loaded lazily, one per language, from the checkpoints the
    upstream project ships (``english``, ``spanish``, ``french``, ...). No
    model is loaded until a voice in that language is used, so the engine
    never pulls the English checkpoint just because English is the default.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        # Per-language model cache: ISO 639-1 code -> loaded TTSModel.
        self._models: dict[str, Any] = {}
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

    @property
    def default_language(self) -> str:
        """Language used when a request does not name one (falls back to es)."""
        value = (self._settings.POCKET_TTS_DEFAULT_LANGUAGE or "").strip().lower()
        return value if value in _POCKET_LANGUAGE_MODELS else "es"

    def _language_kwargs(self, language: str) -> dict[str, Any]:
        """Traduce un código ISO 639-1 a los kwargs de ``TTSModel.load_model``.

        Usa ``POCKET_TTS_MODEL`` (config YAML/ruta/URL) si está fijado, para
        permitir checkpoints locales; si no, selecciona el checkpoint del
        idioma pedido con ``language=``.

        Args:
            language: Código ISO 639-1 del idioma a cargar.

        Returns:
            Argumentos de keyword para ``TTSModel.load_model``.
        """
        override = self._model_override_kwargs()
        if override:
            return override
        return {"language": _POCKET_LANGUAGE_MODELS.get(language, "english")}

    def _model_override_kwargs(self) -> dict[str, Any]:
        """Traduce ``POCKET_TTS_MODEL`` a argumentos válidos de ``load_model``.

        Acepta una ruta/URL de config YAML (``hf://repo/config.yaml``) como
        ``config=``; un repo id de Hugging Face sin ``.yaml`` se ignora.

        Returns:
            Argumentos de keyword para ``TTSModel.load_model``; vacío si el
            valor no es utilizable o no está fijado.
        """
        value = (self._settings.POCKET_TTS_MODEL or "").strip()
        if not value:
            return {}
        if value.startswith(("hf://", "http://", "https://")) or value.endswith((".yaml", ".yml")):
            return {"config": value}
        if Path(value).exists():
            return {"config": value}
        if "/" not in value and "\\" not in value:
            # A bare language name is handled per-request, not as an override.
            return {}
        logger.warning(
            "POCKET_TTS_MODEL=%r no es ni un codigo de idioma ni un fichero .yaml: "
            "se ignora y se usa el checkpoint por idioma.",
            value,
        )
        return {}

    def _model_kwargs(self) -> dict[str, Any]:
        """Compatibilidad: kwargs del override global de modelo (vacío si no hay).

        Returns:
            Argumentos de ``load_model`` derivados de ``POCKET_TTS_MODEL``.
        """
        return self._model_override_kwargs()

    async def _get_model(self, language: str) -> Any:
        """Devuelve el modelo del idioma, cargándolo de forma perezosa.

        Args:
            language: Código ISO 639-1 del idioma.

        Returns:
            El ``TTSModel`` cargado para ese idioma.

        Raises:
            EngineUnavailableError: Si ``pocket-tts`` no está instalado o el
                modelo no se puede cargar.
        """
        cached = self._models.get(language)
        if cached is not None:
            return cached
        try:
            from pocket_tts import TTSModel
        except ImportError:
            raise EngineUnavailableError(
                "pocket-tts no está instalado. Ejecuta: pip install pocket-tts"
            ) from None
        kwargs = self._language_kwargs(language)
        try:
            logger.info(
                "Loading Pocket TTS model for language=%s (%s)...",
                language,
                kwargs or "defaults",
            )
            model = await asyncio.to_thread(TTSModel.load_model, **kwargs)
        except Exception as e:
            raise EngineUnavailableError(f"Error cargando Pocket TTS para {language}: {e}") from e
        self._models[language] = model
        logger.info("Pocket TTS model for language=%s loaded", language)
        return model

    def _reference_path(self, reference_key: str) -> Path | None:
        """Resuelve la ruta de un prompt de referencia ES/FR configurado.

        Args:
            reference_key: Clave lógica (p.ej. ``es_male``) definida en
                ``POCKET_TTS_REFERENCES``.

        Returns:
            La ruta al WAV de referencia, o ``None`` si no está configurada o
            el fichero no existe.
        """
        references = self._settings.pocket_tts_references
        raw = references.get(reference_key)
        if not raw:
            return None
        path = Path(raw)
        return path if path.exists() else None

    async def initialize(self) -> None:
        """Precarga el modelo del idioma por defecto y los prompts stock.

        El resto de idiomas se cargan de forma perezosa en el primer uso. No
        se carga el checkpoint inglés salvo que el idioma por defecto sea
        ``en``.
        """
        language = self.default_language
        await self._get_model(language)
        # Pre-load voice states for stock presets of the default language.
        for voice_id, voice in _STOCK_VOICE_MAP.items():
            if voice["language"] != language:
                continue
            try:
                state = await self._voice_state(voice_id)
                if state is not None:
                    self._voice_states[voice_id] = state
            except Exception as e:
                logger.warning("Could not load stock voice '%s': %s", voice_id, e)
        logger.info("Pocket TTS engine initialized (default language=%s)", language)

    async def warmup(self) -> None:
        """Reintenta la carga del idioma por defecto si initialize() no llegó.

        El warmup se ejecuta tras el arranque del lifespan; si la descarga del
        checkpoint falló (red, disco) el gateway recupera el modelo aquí sin
        reinicio. Ya cargado es un no-op barato.
        """
        if self._models:
            return
        logger.info("Pocket TTS warmup: recargando el idioma por defecto")
        await self.initialize()

    async def _voice_state(self, voice_id: str) -> Any:
        """Estado de voz stock: preset upstream o prompt de referencia clonado.

        Args:
            voice_id: Id de voz stock.

        Returns:
            El estado de audio de pocket_tts, o ``None`` si la voz necesita un
            fichero de referencia que no está configurado.

        Raises:
            VoiceNotFoundError: Si el id no está en el catálogo stock.
        """
        voice = _STOCK_VOICE_MAP.get(voice_id)
        if not voice:
            err = VoiceNotFoundError(voice_id, "stock")
            err.valid_voice_ids = list(_STOCK_VOICE_MAP.keys())
            raise err
        preset = voice.get("preset")
        if preset:
            model = await self._get_model(voice["language"])
            return await asyncio.to_thread(model.get_state_for_audio_prompt, preset)
        reference_key = voice.get("reference")
        if reference_key:
            path = self._reference_path(reference_key)
            if path is None:
                return None
            return await self._get_model_and_state_from_reference(voice["language"], path)
        return None

    async def _get_model_and_state_from_reference(self, language: str, path: Path) -> Any:
        """Carga el modelo del idioma y calcula el estado desde un WAV de referencia."""
        model = await self._get_model(language)

        def _build() -> Any:
            with self._generate_lock:
                return model.get_state_for_audio_prompt(str(path))

        return await asyncio.to_thread(_build)

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
        voice = _STOCK_VOICE_MAP.get(voice_id)
        if not voice:
            valid_ids = list(_STOCK_VOICE_MAP.keys())
            err = VoiceNotFoundError(voice_id, "stock")
            err.valid_voice_ids = valid_ids
            raise err

        voice_language = voice["language"]
        if language not in _POCKET_LANGUAGES:
            raise UnsupportedLanguageError(language, list(_POCKET_LANGUAGES))

        # The voice's own language wins: a voice only renders correctly with
        # the model it was recorded/designed for.
        language = voice_language

        state = self._voice_states.get(voice_id)
        if state is None:
            state = await self._voice_state(voice_id)
            if state is not None:
                self._voice_states[voice_id] = state
        if state is None:
            raise EngineUnavailableError(
                f"Voz '{voice_id}' requiere un fichero de referencia "
                f"({voice.get('reference')}) no configurado"
            )

        model = await self._get_model(language)
        try:
            return await asyncio.to_thread(self._render_locked, model, state, text, speed)
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
        engine_name: str | None = None,
    ) -> bytes:
        if not Path(reference_audio_path).exists():
            raise EngineUnavailableError(f"Reference audio no encontrado: {reference_audio_path}")

        model = await self._get_model(
            language if language in _POCKET_LANGUAGES else self.default_language
        )
        try:
            return await asyncio.to_thread(
                self._render_clone_locked, model, reference_audio_path, text, speed
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
        engine_name: str | None = None,
    ) -> bytes:
        raise FeatureNotSupportedError("instruct/voice design", self.name)

    async def list_stock_voices(self, language: str | None = None) -> list[dict[str, Any]]:
        voices = [
            {"voice_id": vid, "language": v["language"], "gender": v["gender"], "name": v["name"]}
            for vid, v in _STOCK_VOICE_MAP.items()
        ]
        if language:
            voices = [v for v in voices if v["language"] == language]
        return voices

    async def health_check(self) -> dict[str, Any]:
        return {
            "model_loaded": bool(self._models),
            "loaded_languages": sorted(self._models),
            "gpu_available": False,
            "device": "cpu",
            "stock_voices_count": len(_STOCK_VOICE_MAP),
            "vram_free_mb": 0,
            "mode": "REAL" if self._models else "NOT_LOADED",
            "engine": self.name,
        }

    def _render_locked(self, model: Any, state: Any, text: str, speed: float) -> bytes:
        """Genera audio de voz stock serializando el acceso al modelo."""
        with self._generate_lock:
            audio = model.generate_audio(state, text)
        return self._apply_speed(self._tensor_to_wav(model, audio), speed)

    def _render_clone_locked(
        self, model: Any, reference_audio_path: str, text: str, speed: float
    ) -> bytes:
        """Genera audio clonado serializando cálculo de prompt + generación."""
        with self._generate_lock:
            state = self._clone_state(model, reference_audio_path)
            audio = model.generate_audio(state, text)
        return self._apply_speed(self._tensor_to_wav(model, audio), speed)

    def _clone_state(self, model: Any, reference_audio_path: str) -> Any:
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
        state = model.get_state_for_audio_prompt(reference_audio_path)
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

    def _tensor_to_wav(self, model: Any, audio: Any) -> bytes:
        """Convert tensor/numpy output from Pocket TTS to WAV bytes."""
        import numpy as np

        if hasattr(audio, "numpy"):
            audio = audio.numpy()
        audio = np.asarray(audio, dtype=np.float32)

        if audio.ndim > 1:
            audio = audio.flatten()

        sample_rate = getattr(model, "sample_rate", 24000)
        audio_int16 = (audio * 32767).clip(-32768, 32767).astype(np.int16)

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio_int16.tobytes())
        return buffer.getvalue()
