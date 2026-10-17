"""Adapter wrapping the legacy OmniVoiceEngine into the TtsEngineBase interface."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from tts_api.core import ffmpeg
from tts_api.core.engine_base import EngineCapabilities, TtsEngineBase
from tts_api.core.exceptions import EngineUnavailableError
from tts_api.core.omnivoice_engine import (
    OmniVoiceEngine,
    get_engine,
)

logger = logging.getLogger(__name__)

# Sample rate exigido para el audio de referencia de OmniVoice (Hz).
REFERENCE_SAMPLE_RATE = 22050


class OmniVoiceAdapter(TtsEngineBase):
    """Wraps the existing OmniVoiceEngine singleton into the new TtsEngineBase ABC."""

    def __init__(self) -> None:
        self._engine: OmniVoiceEngine | None = None

    @property
    def name(self) -> str:
        return "omnivoice"

    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            stock_voices=True,
            voice_cloning=True,
            instruct_voice_design=True,
            emotions=True,
            streaming=False,
            supported_languages=["es", "en", "fr", "de", "it", "pt", "zh", "ja", "ko"],
        )

    async def initialize(self) -> None:
        self._engine = await get_engine()

    async def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
    ) -> bytes:
        assert self._engine is not None
        return await self._engine.synthesize_stock(
            text=text,
            voice_id=voice_id,
            speed=speed,
            emotion=emotion,
            language=language,
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
        assert self._engine is not None

        # OmniVoice expects 22050Hz reference; resample if stored at 24000Hz
        ref_path = reference_audio_path
        if self._needs_resample(reference_audio_path):
            ref_path = self._resample_to_22050(reference_audio_path)

        try:
            return await self._engine.synthesize_clone(
                text=text,
                reference_audio_path=ref_path,
                ref_text=ref_text,
                speed=speed,
                language=language,
            )
        finally:
            if ref_path != reference_audio_path:
                with contextlib.suppress(Exception):
                    Path(ref_path).unlink()

    @staticmethod
    def _needs_resample(audio_path: str) -> bool:
        try:
            import soundfile as sf

            info = sf.info(audio_path)
            return info.samplerate != REFERENCE_SAMPLE_RATE or info.channels != 1
        except Exception:
            return False

    @staticmethod
    def _resample_to_22050(audio_path: str) -> str:
        """Resample reference audio to 22050Hz mono. Returns temp file path."""
        import tempfile

        import soundfile as sf

        data, sr = sf.read(audio_path, dtype="float32")
        if data.ndim > 1:
            data = data.mean(axis=1)
        if sr != REFERENCE_SAMPLE_RATE:
            data = ffmpeg.resample(data, sr, REFERENCE_SAMPLE_RATE)
            if data is None:
                raise EngineUnavailableError(
                    "No se puede remuestrear la referencia a 22050Hz: falta el binario 'ffmpeg'"
                )

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            sf.write(tmp.name, data, REFERENCE_SAMPLE_RATE, subtype="PCM_16")
            return tmp.name

    async def synthesize_instruct(
        self,
        *,
        text: str,
        instruct: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
    ) -> bytes:
        assert self._engine is not None
        return await self._engine.synthesize_instruct(
            text=text,
            instruct=instruct,
            speed=speed,
            emotion=emotion,
            language=language,
        )

    async def list_stock_voices(self, language: str | None = None) -> list[dict]:
        assert self._engine is not None
        return await self._engine.list_stock_voices(language=language)

    async def health_check(self) -> dict:
        assert self._engine is not None
        return await self._engine.health_check()

    async def warmup(self) -> None:
        if self._engine is not None:
            await self._engine.warmup()

    async def close(self) -> None:
        from tts_api.core.omnivoice_engine import close_engine

        await close_engine()
        self._engine = None
