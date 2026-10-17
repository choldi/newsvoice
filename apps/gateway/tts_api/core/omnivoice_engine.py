"""Interfaz y implementación del motor OmniVoice."""

from __future__ import annotations

import asyncio
import io
import logging
import math
import struct
import wave
from dataclasses import dataclass
from typing import Any, Protocol

import torch

from tts_api.core.exceptions import (
    EngineUnavailableError,
    UnsupportedInstructError,
    VoiceNotFoundError,
)
from tts_api.settings import get_settings

logger = logging.getLogger(__name__)


# --- Tokens válidos para instructs de OmniVoice ---
# El modelo SOLO acepta estos tokens exactos (en inglés o chino).
VALID_INSTRUCT_TOKENS_EN: list[str] = [
    "male",
    "female",
    "child",
    "teenager",
    "young adult",
    "middle-aged",
    "elderly",
    "very low pitch",
    "low pitch",
    "moderate pitch",
    "high pitch",
    "very high pitch",
    "whisper",
    "american accent",
    "australian accent",
    "british accent",
    "canadian accent",
    "chinese accent",
    "indian accent",
    "japanese accent",
    "korean accent",
    "portuguese accent",
    "russian accent",
    "spanish accent",
]

# --- Emotion tags (ModelsLab/omnivoice-singing) ---
# Se aplican como prefijos de texto: "[happy] Hello!" → modelo genera con emoción.
SUPPORTED_EMOTIONS: list[str] = [
    "happy",
    "sad",
    "angry",
    "excited",
    "calm",
    "nervous",
    "whisper",
    "singing",
]

# Memoria libre mínima (MB) requerida al validar un dispositivo CUDA.
_MIN_CUDA_FREE_MB = 100
# guidance_scale por defecto y el que se usa al solicitar una emoción.
_GUIDANCE_SCALE_DEFAULT = 2.0

# Mapeo de emociones a tags del modelo (formato [tag]).
_EMOTION_TAG_MAP: dict[str, str] = {
    "happy": "[happy]",
    "sad": "[sad]",
    "angry": "[angry]",
    "excited": "[excited]",
    "calm": "[calm]",
    "nervous": "[nervous]",
    "whisper": "[whisper]",
    "singing": "[singing]",
}

# Mapeo de voces stock a instructs válidos para model.generate(instruct=...).
STOCK_VOICE_INSTRUCTS: dict[str, str] = {
    "es-mx-male": "male, spanish accent",
    "es-mx-female": "female, spanish accent",
    "es-es-male": "male, spanish accent",
    "es-es-female": "female, spanish accent",
    "en-us-male": "male, american accent",
    "en-us-female": "female, american accent",
    "en-gb-male": "male, british accent",
    "en-gb-female": "female, british accent",
    "fr-fr-male": "male",
    "fr-fr-female": "female",
    "de-de-male": "male",
    "de-de-female": "female",
    "it-it-male": "male",
    "it-it-female": "female",
    "pt-br-male": "male, portuguese accent",
    "pt-br-female": "female, portuguese accent",
    "zh-cn-male": "男",
    "zh-cn-female": "女",
    "ja-jp-male": "male, japanese accent",
    "ja-jp-female": "female, japanese accent",
    "ko-kr-male": "male, korean accent",
    "ko-kr-female": "female, korean accent",
}

# Tokens chinos válidos (separados por coma full-width).
VALID_INSTRUCT_TOKENS_ZH: list[str] = [
    "男",
    "女",
    "儿童",
    "少年",
    "青年",
    "中年",
    "老年",
    "极低音调",
    "低音调",
    "中音调",
    "高音调",
    "极高音调",
    "耳语",
    "河南话",
    "陕西话",
    "四川话",
    "贵州话",
    "云南话",
    "桂林话",
    "济南话",
    "石家庄话",
    "甘肃话",
    "宁夏话",
    "青岛话",
    "东北话",
]

# Mapeo de códigos ISO 639-1 a nombres de idioma completos (para model.generate(language=...))
_LANGUAGE_MAP: dict[str, str] = {
    "es": "Spanish",
    "en": "English",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "ru": "Russian",
    "hi": "Hindi",
}


def _map_language(language: str | None) -> str | None:
    """Mapea un código ISO 639-1 al nombre completo que el modelo espera."""
    if not language:
        return None
    return _LANGUAGE_MAP.get(language.lower(), language)


def _estimate_duration(text: str, speed: float = 1.0) -> float:
    """Estimate required audio duration from text length.

    The OmniVoice model truncates output when no duration is set.
    ~0.08s per character at speed=1.0, with a 3s minimum.
    """
    return max(3.0, len(text) * 0.08 / max(speed, 0.5))


def _apply_emotion(text: str, emotion: str | None) -> str:
    """Aplica un tag de emoción como prefijo al texto.

    El modelo (ModelsLab/omnivoice-singing) procesa tags como [happy], [sad], etc.
    como prefijos del texto. Si el texto ya contiene el tag, no se duplica.
    """
    if not emotion:
        return text
    tag = _EMOTION_TAG_MAP.get(emotion.lower())
    if tag is None:
        return text
    if tag in text:
        return text
    return f"{tag} {text}"


def _validate_instruct(instruct: str) -> None:
    """Valida que el instruct contenga solo tokens soportados por el modelo."""
    tokens = [t.strip().lower() for t in instruct.split(",")]
    # Normalizar tokens chinos (full-width comma → half-width)
    all_valid = {t.lower() for t in VALID_INSTRUCT_TOKENS_EN} | set(VALID_INSTRUCT_TOKENS_ZH)
    invalid: dict[str, str | None] = {}

    for token in tokens:
        if token and token not in all_valid:
            suggestion = None
            for valid in list(VALID_INSTRUCT_TOKENS_EN) + VALID_INSTRUCT_TOKENS_ZH:
                if valid in token or token in valid:
                    suggestion = valid
                    break
            invalid[token] = suggestion

    if invalid:
        raise UnsupportedInstructError(
            instruct, invalid, list(VALID_INSTRUCT_TOKENS_EN) + VALID_INSTRUCT_TOKENS_ZH
        )


@dataclass
class GenerationParams:
    """Parámetros de generación de OmniVoice.

    Todos los campos son opcionales. Los valores por defecto coinciden con
    los de OmniVoice.
    """

    num_step: int = 32
    denoise: bool = True
    guidance_scale: float = 2.0
    duration: float | None = None
    preprocess_prompt: bool = False
    postprocess_output: bool = True
    pad_duration: float = 0.1
    fade_duration: float = 0.1
    audio_chunk_duration: float = 15.0
    audio_chunk_threshold: float = 30.0

    def to_kwargs(self) -> dict[str, Any]:
        """Convierte a kwargs para model.generate(), omitiendo None/defaults."""
        result: dict[str, Any] = {}
        result["num_step"] = self.num_step
        result["denoise"] = self.denoise
        result["guidance_scale"] = self.guidance_scale
        if self.duration is not None:
            result["duration"] = self.duration
        result["preprocess_prompt"] = self.preprocess_prompt
        result["postprocess_output"] = self.postprocess_output
        result["pad_duration"] = self.pad_duration
        result["fade_duration"] = self.fade_duration
        result["audio_chunk_duration"] = self.audio_chunk_duration
        result["audio_chunk_threshold"] = self.audio_chunk_threshold
        return result


class OmniVoiceEngineInterface(Protocol):
    """Protocolo para el motor de síntesis."""

    async def synthesize_stock(
        self,
        *,
        text: str,
        voice_id: str,
        speed: float = 1.0,
        emotion: str | None = None,
        generation_params: GenerationParams | None = None,
    ) -> bytes: ...

    async def synthesize_instruct(
        self,
        *,
        text: str,
        instruct: str,
        speed: float = 1.0,
        emotion: str | None = None,
        generation_params: GenerationParams | None = None,
    ) -> bytes: ...

    async def synthesize_clone(
        self,
        *,
        text: str,
        reference_audio_path: str,
        instruct: str | None = None,
        speed: float = 1.0,
        emotion: str | None = None,
        generation_params: GenerationParams | None = None,
    ) -> bytes: ...

    async def list_stock_voices(self, language: str | None = None) -> list[dict]: ...

    async def health_check(self) -> dict: ...

    async def warmup(self) -> None: ...


def _get_dtype() -> torch.dtype:
    """Convierte el string de dtype a torch.dtype."""
    settings = get_settings()
    dtype_map = {
        "float16": torch.float16,
        "float32": torch.float32,
        "int8": torch.int8,
    }
    return dtype_map.get(settings.TTS_DTYPE, torch.float16)


def _validate_cuda_device(device: str) -> None:
    """Valida que el dispositivo CUDA especificado sea coherente con la disponibilidad de GPU."""
    if not device.startswith("cuda"):
        return

    if not torch.cuda.is_available():
        raise EngineUnavailableError(
            f"Se solicitó dispositivo CUDA '{device}' pero torch.cuda.is_available() es False. "
            "Verifica que CUDA esté instalado y que la GPU sea accesible."
        )

    try:
        device_index = int(device.split(":")[1]) if ":" in device else 0
    except (ValueError, IndexError):
        raise EngineUnavailableError(
            f"Formato de dispositivo CUDA inválido: '{device}'. "
            "Use formato 'cuda:X' donde X es el índice del dispositivo (ej. 'cuda:0')."
        ) from None

    device_count = torch.cuda.device_count()
    if device_index >= device_count:
        raise EngineUnavailableError(
            f"Dispositivo CUDA '{device}' no existe. "
            f"Dispositivos disponibles: 0 a {device_count - 1} (total: {device_count})."
        )

    try:
        free_mem, total_mem = torch.cuda.mem_get_info(device_index)
        free_mb = free_mem // (1024 * 1024)
        if free_mb < _MIN_CUDA_FREE_MB:
            raise EngineUnavailableError(
                f"Dispositivo CUDA '{device}' tiene muy poca memoria libre: {free_mb} MB. "
                f"Se requieren al menos {_MIN_CUDA_FREE_MB} MB libres. "
                f"Memoria total: {total_mem // (1024 * 1024)} MB."
            )
    except Exception as e:
        logger.warning("No se pudo verificar memoria de CUDA device %s: %s", device, e)


class OmniVoiceEngine:
    """Implementación del motor OmniVoice usando la API de Python directamente."""

    _instance: OmniVoiceEngine | None = None
    _initialized: bool = False

    def __new__(cls) -> OmniVoiceEngine:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True

        self._settings = get_settings()
        self._device = self._settings.TTS_DEVICE
        self._model = None
        self._stock_voices: list[dict] = []
        self._use_mock: bool = self._settings.TTS_USE_MOCK
        self._real_engine_error: str | None = None

    def _activate_mock_mode(self, reason: str | None = None) -> None:
        """Conmuta el engine a modo mock (tonos de prueba)."""
        self._use_mock = True
        self._model = None
        if reason:
            self._real_engine_error = reason
        self._stock_voices = self._get_mock_stock_voices()

    def _resolve_model_source(self) -> str:
        """Resuelve la fuente del modelo: directorio local o HuggingFace Hub ID.

        Prioridad:
          1. {TTS_INSTALL_DIR}/models (descargado por make download-model)
          2. TTS_MODEL_ID (HuggingFace Hub ID, descarga bajo demanda)
        """
        local_path = self._settings.model_path
        if local_path.is_dir() and (local_path / "config.json").exists():
            return str(local_path)

        return self._settings.TTS_MODEL_ID

    async def initialize(self) -> None:
        """Inicializa el modelo."""
        if self._model is not None:
            return

        logger.info("Initializing OmniVoice engine...")
        self._settings = get_settings()
        self._use_mock = self._settings.TTS_USE_MOCK
        self._real_engine_error = None
        logger.info("TTS_USE_MOCK=%s", self._use_mock)

        if not self._use_mock:
            try:
                _validate_cuda_device(self._settings.TTS_DEVICE)
                logger.info("Validación CUDA OK: device=%s", self._settings.TTS_DEVICE)
            except EngineUnavailableError as e:
                if self._settings.TTS_FALLBACK_TO_MOCK:
                    logger.error(
                        "Validación CUDA falló: %s. "
                        "TTS_FALLBACK_TO_MOCK=true → conmutando a modo MOCK.",
                        e,
                    )
                    self._activate_mock_mode(reason=str(e))
                    await self.warmup()
                    logger.info("Mock engine inicializado como fallback tras fallo CUDA")
                    return
                raise

        if self._use_mock:
            logger.warning("MODO MOCK: generando tonos de prueba")
            self._activate_mock_mode()
            await self.warmup()
            logger.info("Mock engine inicializado")
            return

        logger.info("Modo REAL: cargando OmniVoice...")
        try:
            from omnivoice import OmniVoice

            # Prefer local directory if it exists (downloaded by make download-model)
            model_source = self._resolve_model_source()
            logger.info("Cargando modelo desde: %s", model_source)
            self._model = OmniVoice.from_pretrained(
                model_source,
                device_map=self._settings.TTS_DEVICE,
                dtype=_get_dtype(),
            )
            self._stock_voices = self._get_mock_stock_voices()
            await self.warmup()
            logger.info("Engine OmniVoice REAL inicializado")
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            if self._settings.TTS_FALLBACK_TO_MOCK:
                logger.error(
                    "Fallo al cargar el motor REAL: %s. Conmutando a MOCK.",
                    error_msg,
                )
                self._activate_mock_mode(reason=error_msg)
                await self.warmup()
                return
            raise EngineUnavailableError(
                f"No se pudo cargar el modelo OmniVoice: {error_msg}"
            ) from e

    async def warmup(self) -> None:
        """Verifica que el modelo responde."""
        if self._use_mock:
            await asyncio.sleep(0.01)
            return
        logger.info("Warmup: probando síntesis...")
        try:
            audio = self._model.generate(text=".", num_step=32)
            if audio and len(audio) > 0:
                logger.info("Warmup OK")
            else:
                logger.warning("Warmup: audio vacío")
        except Exception as e:
            logger.warning("Warmup falló: %s", e)

    def _get_mock_stock_voices(self) -> list[dict]:
        """Voces stock mock."""
        return [
            {
                "voice_id": "es-mx-male",
                "language": "es",
                "gender": "male",
                "name": "Spanish MX Male",
            },
            {
                "voice_id": "es-mx-female",
                "language": "es",
                "gender": "female",
                "name": "Spanish MX Female",
            },
            {
                "voice_id": "es-es-male",
                "language": "es",
                "gender": "male",
                "name": "Spanish Spain Male",
            },
            {
                "voice_id": "es-es-female",
                "language": "es",
                "gender": "female",
                "name": "Spanish Spain Female",
            },
            {
                "voice_id": "en-us-male",
                "language": "en",
                "gender": "male",
                "name": "English US Male",
            },
            {
                "voice_id": "en-us-female",
                "language": "en",
                "gender": "female",
                "name": "English US Female",
            },
            {
                "voice_id": "en-gb-male",
                "language": "en",
                "gender": "male",
                "name": "English UK Male",
            },
            {
                "voice_id": "en-gb-female",
                "language": "en",
                "gender": "female",
                "name": "English UK Female",
            },
            {"voice_id": "fr-fr-male", "language": "fr", "gender": "male", "name": "French Male"},
            {
                "voice_id": "fr-fr-female",
                "language": "fr",
                "gender": "female",
                "name": "French Female",
            },
            {"voice_id": "de-de-male", "language": "de", "gender": "male", "name": "German Male"},
            {
                "voice_id": "de-de-female",
                "language": "de",
                "gender": "female",
                "name": "German Female",
            },
            {"voice_id": "it-it-male", "language": "it", "gender": "male", "name": "Italian Male"},
            {
                "voice_id": "it-it-female",
                "language": "it",
                "gender": "female",
                "name": "Italian Female",
            },
            {
                "voice_id": "pt-br-male",
                "language": "pt",
                "gender": "male",
                "name": "Portuguese BR Male",
            },
            {
                "voice_id": "pt-br-female",
                "language": "pt",
                "gender": "female",
                "name": "Portuguese BR Female",
            },
            {"voice_id": "zh-cn-male", "language": "zh", "gender": "male", "name": "Chinese Male"},
            {
                "voice_id": "zh-cn-female",
                "language": "zh",
                "gender": "female",
                "name": "Chinese Female",
            },
            {"voice_id": "ja-jp-male", "language": "ja", "gender": "male", "name": "Japanese Male"},
            {
                "voice_id": "ja-jp-female",
                "language": "ja",
                "gender": "female",
                "name": "Japanese Female",
            },
            {"voice_id": "ko-kr-male", "language": "ko", "gender": "male", "name": "Korean Male"},
            {
                "voice_id": "ko-kr-female",
                "language": "ko",
                "gender": "female",
                "name": "Korean Female",
            },
        ]

    def _get_instruct_for_voice(self, voice_id: str) -> str:
        """Obtiene el instruct válido para una voz stock."""
        instruct = STOCK_VOICE_INSTRUCTS.get(voice_id)
        if instruct is None:
            list(STOCK_VOICE_INSTRUCTS.keys())
            raise VoiceNotFoundError(voice_id, "stock")
        _validate_instruct(instruct)
        return instruct

    def _numpy_to_wav(self, audio: list) -> bytes:
        """Convierte lista de numpy arrays a WAV bytes (24 kHz, mono)."""
        import numpy as np

        if not audio or len(audio) == 0:
            raise EngineUnavailableError("El modelo no generó audio")

        audio_data = np.concatenate(audio) if len(audio) > 1 else audio[0]

        if audio_data.dtype != np.float32:
            audio_data = audio_data.astype(np.float32)

        audio_int16 = (audio_data * 32767).astype(np.int16)

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            wav_file.writeframes(audio_int16.tobytes())

        return buffer.getvalue()

    async def synthesize_stock(
        self,
        *,
        text: str,
        voice_id: str,
        speed: float = 1.0,
        emotion: str | None = None,
        generation_params: GenerationParams | None = None,
        language: str | None = None,
    ) -> bytes:
        """Sintetiza con voz stock usando voice design."""
        logger.debug(
            "synthesize_stock: voice_id=%s, text_len=%d, emotion=%s", voice_id, len(text), emotion
        )

        voice = next((v for v in self._stock_voices if v["voice_id"] == voice_id), None)
        if not voice:
            raise VoiceNotFoundError(voice_id, "stock")

        if self._use_mock:
            return self._generate_test_tone_wav(
                duration_sec=max(0.5, len(text) * 0.08),
                sample_rate=22050,
                frequency=440.0,
                amplitude=0.3,
            )

        instruct = self._get_instruct_for_voice(voice_id)
        params = generation_params or GenerationParams()

        # Auto-estimate duration if not explicitly set — prevents text truncation
        if params.duration is None:
            params.duration = _estimate_duration(text, speed)

        try:
            kwargs = params.to_kwargs()
            kwargs["text"] = _apply_emotion(text, emotion)
            kwargs["instruct"] = instruct
            kwargs["speed"] = speed
            lang = _map_language(language)
            if lang:
                kwargs["language"] = lang
            if emotion and (
                kwargs.get("guidance_scale", _GUIDANCE_SCALE_DEFAULT) == _GUIDANCE_SCALE_DEFAULT
            ):
                kwargs["guidance_scale"] = 3.0
            audio = await asyncio.to_thread(self._model.generate, **kwargs)
            return self._numpy_to_wav(audio)
        except Exception as e:
            logger.error("Error en synthesize_stock: %s", e)
            raise EngineUnavailableError(f"Error sintetizando: {e}") from e

    async def synthesize_instruct(
        self,
        *,
        text: str,
        instruct: str,
        speed: float = 1.0,
        emotion: str | None = None,
        generation_params: GenerationParams | None = None,
        language: str | None = None,
    ) -> bytes:
        """Sintetiza con instruct personalizado (voice design libre)."""
        logger.debug(
            "synthesize_instruct: instruct=%s, text_len=%d, emotion=%s",
            instruct,
            len(text),
            emotion,
        )

        _validate_instruct(instruct)

        if self._use_mock:
            return self._generate_test_tone_wav(
                duration_sec=max(0.5, len(text) * 0.08),
                sample_rate=22050,
                frequency=440.0,
                amplitude=0.3,
            )

        params = generation_params or GenerationParams()

        # Auto-estimate duration if not explicitly set — prevents text truncation
        if params.duration is None:
            params.duration = _estimate_duration(text, speed)

        try:
            kwargs = params.to_kwargs()
            kwargs["text"] = _apply_emotion(text, emotion)
            kwargs["instruct"] = instruct
            kwargs["speed"] = speed
            lang = _map_language(language)
            if lang:
                kwargs["language"] = lang
            if emotion and (
                kwargs.get("guidance_scale", _GUIDANCE_SCALE_DEFAULT) == _GUIDANCE_SCALE_DEFAULT
            ):
                kwargs["guidance_scale"] = 3.0
            audio = await asyncio.to_thread(self._model.generate, **kwargs)
            return self._numpy_to_wav(audio)
        except Exception as e:
            logger.error("Error en synthesize_instruct: %s", e)
            raise EngineUnavailableError(f"Error sintetizando: {e}") from e

    async def synthesize_clone(
        self,
        *,
        text: str,
        reference_audio_path: str,
        ref_text: str | None = None,
        instruct: str | None = None,
        speed: float = 1.0,
        emotion: str | None = None,
        generation_params: GenerationParams | None = None,
        language: str | None = None,
    ) -> bytes:
        """Sintetiza con voz clonada, opcionalmente con instruct."""
        logger.debug(
            "synthesize_clone: ref=%s, text_len=%d, emotion=%s",
            reference_audio_path,
            len(text),
            emotion,
        )

        from pathlib import Path

        if not self._use_mock and not Path(reference_audio_path).exists():
            raise EngineUnavailableError(f"Reference audio no encontrado: {reference_audio_path}")

        if self._use_mock:
            return self._generate_test_tone_wav(
                duration_sec=max(0.5, len(text) * 0.08),
                sample_rate=22050,
                frequency=880.0,
                amplitude=0.3,
            )

        params = generation_params or GenerationParams()

        # Auto-estimate duration if not explicitly set — prevents text truncation
        if params.duration is None:
            params.duration = _estimate_duration(text, speed)

        # Clone-specific tuning: smoother fade-in to suppress initial artifact,
        # higher steps for better voice fidelity
        params.fade_duration = max(params.fade_duration, 0.3)
        params.num_step = max(params.num_step, 48)

        try:
            kwargs = params.to_kwargs()
            kwargs["text"] = _apply_emotion(text, emotion)
            kwargs["ref_audio"] = reference_audio_path
            if ref_text:
                kwargs["ref_text"] = ref_text
            kwargs["speed"] = speed
            lang = _map_language(language)
            if lang:
                kwargs["language"] = lang
            if emotion and (
                kwargs.get("guidance_scale", _GUIDANCE_SCALE_DEFAULT) == _GUIDANCE_SCALE_DEFAULT
            ):
                kwargs["guidance_scale"] = 3.0
            if instruct:
                _validate_instruct(instruct)
                kwargs["instruct"] = instruct
            audio = await asyncio.to_thread(self._model.generate, **kwargs)
            return self._numpy_to_wav(audio)
        except Exception as e:
            logger.error("Error en synthesize_clone: %s", e)
            raise EngineUnavailableError(f"Error sintetizando con voz clonada: {e}") from e

    async def list_stock_voices(self, language: str | None = None) -> list[dict]:
        """Lista voces stock."""
        voices = self._stock_voices
        if language:
            voices = [v for v in voices if v["language"] == language]
        return voices

    async def health_check(self) -> dict:
        """Estado del motor."""
        gpu_available = False
        vram_free_mb = 0
        device_info = "cpu"
        settings = self._settings

        try:
            gpu_available = torch.cuda.is_available()
            if gpu_available:
                device_str = getattr(settings, "TTS_DEVICE", "cuda:0")
                if device_str.startswith("cuda"):
                    try:
                        device_idx = int(device_str.split(":")[1]) if ":" in device_str else 0
                        vram_free_mb = torch.cuda.mem_get_info(device_idx)[0] // (1024 * 1024)
                        device_info = f"cuda:{device_idx}"
                    except Exception:
                        vram_free_mb = torch.cuda.mem_get_info()[0] // (1024 * 1024)
                        device_info = f"cuda:{torch.cuda.current_device()}"
                else:
                    vram_free_mb = torch.cuda.mem_get_info()[0] // (1024 * 1024)
                    device_info = f"cuda:{torch.cuda.current_device()}"
        except Exception:
            pass

        return {
            "model_loaded": self._model is not None or self._use_mock,
            "gpu_available": gpu_available,
            "device": device_info,
            "stock_voices_count": len(self._stock_voices),
            "vram_free_mb": vram_free_mb,
            "mode": "MOCK" if self._use_mock else "REAL",
            "real_engine_error": self._real_engine_error,
        }

    def _generate_test_tone_wav(
        self,
        duration_sec: float = 1.0,
        sample_rate: int = 22050,
        frequency: float = 440.0,
        amplitude: float = 0.3,
    ) -> bytes:
        """Genera WAV con tono senoidal (para modo mock)."""
        num_samples = int(duration_sec * sample_rate)
        max_amplitude = 32767

        samples = []
        for i in range(num_samples):
            t = i / sample_rate
            value = amplitude * math.sin(2 * math.pi * frequency * t)
            sample = int(max_amplitude * value)
            samples.append(struct.pack("<h", sample))

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(b"".join(samples))

        return buffer.getvalue()


_engine_instance: OmniVoiceEngine | None = None


async def get_engine() -> OmniVoiceEngine:
    """Obtiene la instancia singleton del motor."""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = OmniVoiceEngine()
        await _engine_instance.initialize()
    return _engine_instance


async def close_engine() -> None:
    """Cierra el motor."""
    global _engine_instance
    if _engine_instance is not None:
        _engine_instance = None
