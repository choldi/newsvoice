"""Kokoro TTS engine (hexgrad/Kokoro-82M) — CPU-first, stock voices only.

Kokoro is an 82M-parameter open-weight TTS model with a small set of stock
voices per language (American/British English, Spanish, French, Hindi,
Italian, Portuguese, ...). It does NOT support voice cloning, instruct/voice
design nor emotions, and has no Catalan.

The pipeline loads lazily on the first synthesis so a cold API start does not
download weights nor occupy memory until Kokoro is actually selected by the
router. The heavy ``kokoro`` import (which pulls torch/transformers/spacy) is
deferred to that same point.
"""

from __future__ import annotations

import asyncio
import io
import logging
import threading
import wave
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

# Frecuencia de muestreo de Kokoro-82M.
_SAMPLE_RATE = 24000

# Idioma del gateway -> ``lang_code`` de una letra que espera KPipeline.
# Kokoro no soporta catalán.
_KOKORO_LANGUAGE_CODES: dict[str, str] = {
    "en": "a",
    "en-us": "a",
    "en-gb": "b",
    "es": "e",
    "fr": "f",
    "hi": "h",
    "it": "i",
    "pt": "p",
}

# Idiomas declarados como soportados (los del mapping anterior, agrupados).
_SUPPORTED_LANGUAGES = ["en", "es", "fr", "hi", "it", "pt"]

# Voces stock de Kokoro por defecto para nuestro esquema de voice_id. El
# prefijo de la voz nativa codifica idioma + género (p.ej. ``ef_dora``:
# e=español, f=femenino). Se puede sobrescribir con ``KOKORO_VOICES``.
# Nota: ``hexgrad/Kokoro-82M`` solo publica UNA voz francesa (``ff_siwis``);
# no existe ``fm_gabriel`` (HF responde 404 en la síntesis), así que
# ``fr-fr-male`` queda fuera de la tabla y el gateway contesta un
# VoiceNotFound tipado en vez de un 500.
_DEFAULT_VOICES: dict[str, dict[str, str]] = {
    "es-mx-male": {"voice": "em_alex", "language": "es", "gender": "male"},
    "es-mx-female": {"voice": "ef_dora", "language": "es", "gender": "female"},
    "es-es-male": {"voice": "em_alex", "language": "es", "gender": "male"},
    "es-es-female": {"voice": "ef_dora", "language": "es", "gender": "female"},
    "en-us-male": {"voice": "am_michael", "language": "en", "gender": "male"},
    "en-us-female": {"voice": "af_heart", "language": "en", "gender": "female"},
    "en-gb-male": {"voice": "bm_george", "language": "en", "gender": "male"},
    "en-gb-female": {"voice": "bf_emma", "language": "en", "gender": "female"},
    "fr-fr-female": {"voice": "ff_siwis", "language": "fr", "gender": "female"},
    "it-it-male": {"voice": "im_nicola", "language": "it", "gender": "male"},
    "it-it-female": {"voice": "if_sara", "language": "it", "gender": "female"},
    "pt-br-male": {"voice": "pm_alex", "language": "pt", "gender": "male"},
    "pt-br-female": {"voice": "pf_dora", "language": "pt", "gender": "female"},
    "hi-in-male": {"voice": "hm_omega", "language": "hi", "gender": "male"},
    "hi-in-female": {"voice": "hf_alpha", "language": "hi", "gender": "female"},
}


class KokoroEngine(TtsEngineBase):
    """Kokoro-82M stock-voice engine."""

    def __init__(self) -> None:
        self._pipelines: dict[str, Any] = {}
        self._load_lock = threading.Lock()
        self._generate_lock = threading.Lock()
        self._current_model = get_settings().KOKORO_REPO_ID

    @property
    def name(self) -> str:
        """Human-readable engine name."""
        return "kokoro"

    @property
    def capabilities(self) -> EngineCapabilities:
        """Declare what this engine supports."""
        return EngineCapabilities(
            stock_voices=True,
            voice_cloning=False,
            instruct_voice_design=False,
            emotions=False,
            streaming=False,
            supported_languages=list(_SUPPORTED_LANGUAGES),
        )

    async def initialize(self) -> None:
        """Load the default-language pipeline (fail fast if deps are missing)."""
        settings = get_settings()
        language = settings.KOKORO_DEFAULT_LANGUAGE or "es"
        await self._ensure_pipeline(language)

    async def warmup(self) -> None:
        """Reload the default pipeline if ``initialize()`` did not get there.

        Runs from the lifespan after startup: a transient failure (network,
        disk) leaves the gateway with an unloaded pipeline, which reports
        ``model_loaded=False`` and degrades a routed gateway. Cheap no-op once
        the pipeline is in memory.
        """
        if self._pipelines:
            return
        logger.info("Kokoro warmup: reloading the default pipeline")
        await self.initialize()

    def _voice_table(self) -> dict[str, dict[str, str]]:
        """Return the voice_id -> voice mapping (settings override wins)."""
        table = {k: dict(v) for k, v in _DEFAULT_VOICES.items()}
        for voice_id, native in get_settings().kokoro_voices.items():
            base = table.get(voice_id, {})
            language = base.get("language") or self._language_from_voice(native)
            table[voice_id] = {
                "voice": native,
                "language": language,
                "gender": base.get("gender", ""),
            }
        return table

    @staticmethod
    def _language_from_voice(voice: str) -> str:
        """Derive the gateway language from a native Kokoro voice id.

        The first letter of a Kokoro voice selects its language group; there is
        no reverse mapping for every group, so unknown groups fall back to the
        default language.
        """
        code = (voice or "")[:1].lower()
        for language, letter in _KOKORO_LANGUAGE_CODES.items():
            if letter == code:
                return language.split("-")[0]
        return get_settings().KOKORO_DEFAULT_LANGUAGE or "es"

    @staticmethod
    def _lang_code(language: str) -> str:
        """Map a gateway language to the single-letter KPipeline lang code."""
        code = _KOKORO_LANGUAGE_CODES.get(language)
        if code is None:
            code = _KOKORO_LANGUAGE_CODES.get(language.split("-", maxsplit=1)[0])
        return code or ""

    def _load_pipeline(self, language: str) -> Any:
        with self._load_lock:
            lang_code = self._lang_code(language)
            if not lang_code:
                raise UnsupportedLanguageError(language, _SUPPORTED_LANGUAGES)
            if lang_code in self._pipelines:
                return self._pipelines[lang_code]

            try:
                from kokoro import KPipeline
            except ImportError as exc:
                raise EngineUnavailableError(
                    "kokoro no está instalado. Instala el extra: uv sync --extra kokoro"
                ) from exc

            repo_id = get_settings().KOKORO_REPO_ID
            try:
                pipeline = KPipeline(lang_code=lang_code, repo_id=repo_id)
            except Exception as exc:
                raise EngineUnavailableError(f"Error cargando Kokoro ({repo_id}): {exc}") from exc

            self._pipelines[lang_code] = pipeline
            logger.info("kokoro pipeline loaded (lang_code=%s, repo=%s)", lang_code, repo_id)
            return pipeline

    async def _ensure_pipeline(self, language: str) -> Any:
        lang_code = self._lang_code(language)
        if lang_code in self._pipelines:
            return self._pipelines[lang_code]
        return await asyncio.to_thread(self._load_pipeline, language)

    def _generate(self, text: str, voice: str, lang_code: str, speed: float) -> Any:
        import numpy as np

        pipeline = self._pipelines[lang_code]
        chunks: list[Any] = []
        with self._generate_lock:
            for _gs, _ps, audio in pipeline(text, voice=voice, speed=speed):
                if audio is None:
                    continue
                chunks.append(np.asarray(_tensor_to_numpy(audio), dtype=np.float32).squeeze())
        if not chunks:
            raise EngineUnavailableError("Kokoro no devolvió audio para el texto dado")
        return np.concatenate(chunks) if len(chunks) > 1 else chunks[0]

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
        """Synthesize text with a stock voice. Returns WAV bytes."""
        table = self._voice_table()
        entry = table.get(voice_id)
        if entry is None:
            raise VoiceNotFoundError(voice_id, "stock")

        entry_language = entry.get("language") or language
        if entry_language not in _SUPPORTED_LANGUAGES:
            raise UnsupportedLanguageError(entry_language, _SUPPORTED_LANGUAGES)

        await self._ensure_pipeline(entry_language)
        lang_code = self._lang_code(entry_language)
        audio = await asyncio.to_thread(self._generate, text, entry["voice"], lang_code, speed)
        return self._numpy_to_wav(audio)

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
        """Kokoro does not support voice cloning."""
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
        """Kokoro does not support instruct/voice design."""
        raise FeatureNotSupportedError("instruct/voice design", self.name)

    async def list_stock_voices(self, language: str | None = None) -> list[dict[str, Any]]:
        """Return available stock voices, optionally filtered by language."""
        voices = [
            {
                "voice_id": voice_id,
                "language": entry["language"],
                "gender": entry.get("gender", ""),
                "name": voice_id,
            }
            for voice_id, entry in self._voice_table().items()
        ]
        if language:
            voices = [v for v in voices if v["language"] == language]
        return voices

    async def health_check(self) -> dict[str, Any]:
        """Return engine health status dict."""
        return {
            "model_loaded": bool(self._pipelines),
            "gpu_available": False,
            "device": "cpu",
            "stock_voices_count": len(await self.list_stock_voices()),
            "vram_free_mb": 0,
            "mode": "REAL" if self._pipelines else "NOT_LOADED",
            "engine": self.name,
            "current_model": self._current_model,
            "loaded_languages": sorted(self._pipelines),
        }

    async def list_available_models(self) -> list[str]:
        """List available models for this engine."""
        return [self._current_model]

    async def list_available_features(self) -> dict[str, Any]:
        """Return detailed feature information."""
        features = self.capabilities.__dict__.copy()
        features.update(
            {
                "available_models": [self._current_model],
                "current_model": self._current_model,
                "supports_cloning": False,
                "supports_instruct": False,
                "supports_emotions": False,
                "supports_streaming": False,
            }
        )
        return features

    async def change_model(self, model_name: str) -> bool:
        """Kokoro ships a single model; only the configured repo is valid."""
        if model_name != self._current_model:
            logger.warning("Kokoro only supports model %s", self._current_model)
            return False
        return True

    def _numpy_to_wav(self, audio: Any) -> bytes:
        """Convert a float32 mono signal to 16-bit PCM WAV bytes."""
        import numpy as np

        pcm = (np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0) * 32767.0).astype(np.int16)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(_SAMPLE_RATE)
            out.writeframes(pcm.tobytes())
        return buffer.getvalue()


def _tensor_to_numpy(audio: Any) -> Any:
    """Detach a torch tensor (or pass a numpy array through)."""
    if hasattr(audio, "detach"):
        audio = audio.detach().cpu().numpy()
    return audio
