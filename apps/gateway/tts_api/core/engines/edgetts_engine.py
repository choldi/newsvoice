"""EdgeTTS engine — Microsoft cloud TTS, 400+ voices including Catalan."""

from __future__ import annotations

import io
import logging
import tempfile
from pathlib import Path

from tts_api.core.engine_base import EngineCapabilities, TtsEngineBase
from tts_api.core.exceptions import (
    EngineUnavailableError,
    FeatureNotSupportedError,
    UnsupportedLanguageError,
    VoiceNotFoundError,
)
from tts_api.settings import get_settings

logger = logging.getLogger(__name__)

# Mapping from our voice_id scheme to Microsoft Edge Neural voice names
_STOCK_VOICE_MAP: dict[str, dict[str, str]] = {
    # Spanish
    "es-mx-male": {
        "ms_voice": "es-MX-JorgeNeural",
        "language": "es",
        "gender": "male",
        "name": "Spanish MX Male",
    },
    "es-mx-female": {
        "ms_voice": "es-MX-DaliaNeural",
        "language": "es",
        "gender": "female",
        "name": "Spanish MX Female",
    },
    "es-es-male": {
        "ms_voice": "es-ES-AlvaroNeural",
        "language": "es",
        "gender": "male",
        "name": "Spanish Spain Male",
    },
    "es-es-female": {
        "ms_voice": "es-ES-ElviraNeural",
        "language": "es",
        "gender": "female",
        "name": "Spanish Spain Female",
    },
    # Catalan
    "ca-male": {
        "ms_voice": "ca-ES-EnricNeural",
        "language": "ca",
        "gender": "male",
        "name": "Catalan Male",
    },
    "ca-female": {
        "ms_voice": "ca-ES-JoanaNeural",
        "language": "ca",
        "gender": "female",
        "name": "Catalan Female",
    },
    # English
    "en-us-male": {
        "ms_voice": "en-US-GuyNeural",
        "language": "en",
        "gender": "male",
        "name": "English US Male",
    },
    "en-us-female": {
        "ms_voice": "en-US-JennyNeural",
        "language": "en",
        "gender": "female",
        "name": "English US Female",
    },
    "en-gb-male": {
        "ms_voice": "en-GB-RyanNeural",
        "language": "en",
        "gender": "male",
        "name": "English UK Male",
    },
    "en-gb-female": {
        "ms_voice": "en-GB-SoniaNeural",
        "language": "en",
        "gender": "female",
        "name": "English UK Female",
    },
    # French
    "fr-fr-male": {
        "ms_voice": "fr-FR-HenriNeural",
        "language": "fr",
        "gender": "male",
        "name": "French Male",
    },
    "fr-fr-female": {
        "ms_voice": "fr-FR-DeniseNeural",
        "language": "fr",
        "gender": "female",
        "name": "French Female",
    },
    # German
    "de-de-male": {
        "ms_voice": "de-DE-ConradNeural",
        "language": "de",
        "gender": "male",
        "name": "German Male",
    },
    "de-de-female": {
        "ms_voice": "de-DE-KatjaNeural",
        "language": "de",
        "gender": "female",
        "name": "German Female",
    },
    # Italian
    "it-it-male": {
        "ms_voice": "it-IT-DiegoNeural",
        "language": "it",
        "gender": "male",
        "name": "Italian Male",
    },
    "it-it-female": {
        "ms_voice": "it-IT-ElsaNeural",
        "language": "it",
        "gender": "female",
        "name": "Italian Female",
    },
    # Portuguese
    "pt-br-male": {
        "ms_voice": "pt-BR-AntonioNeural",
        "language": "pt",
        "gender": "male",
        "name": "Portuguese BR Male",
    },
    "pt-br-female": {
        "ms_voice": "pt-BR-FranciscaNeural",
        "language": "pt",
        "gender": "female",
        "name": "Portuguese BR Female",
    },
}

_EDGETTS_LANGUAGES = {"es", "en", "fr", "de", "it", "pt", "ca"}


class EdgeTTSEngine(TtsEngineBase):
    """Microsoft Edge TTS — cloud-based, 400+ voices, no local compute."""

    def __init__(self) -> None:
        self._settings = get_settings()

    @property
    def name(self) -> str:
        return "edgetts"

    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            stock_voices=True,
            voice_cloning=False,
            instruct_voice_design=False,
            emotions=False,
            streaming=False,
            supported_languages=list(_EDGETTS_LANGUAGES),
        )

    async def initialize(self) -> None:
        try:
            import edge_tts  # noqa: F401

            logger.info("EdgeTTS engine initialized (cloud, no local model)")
        except ImportError:
            raise EngineUnavailableError(
                "edge-tts no está instalado. Ejecuta: pip install edge-tts"
            ) from None

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

        if language not in _EDGETTS_LANGUAGES:
            raise UnsupportedLanguageError(language, list(_EDGETTS_LANGUAGES))

        ms_voice = voice["ms_voice"]
        rate_str = self._speed_to_rate(speed)

        return await self._synthesize_edge(text, ms_voice, rate_str)

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
            "model_loaded": True,
            "gpu_available": False,
            "device": "cloud",
            "stock_voices_count": len(_STOCK_VOICE_MAP),
            "vram_free_mb": 0,
            "mode": "REAL",
            "engine": self.name,
        }

    async def _synthesize_edge(self, text: str, voice: str, rate: str) -> bytes:
        """Call edge-tts and return WAV bytes (converts from MP3)."""
        import edge_tts

        communicate = edge_tts.Communicate(text, voice, rate=rate)

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            await communicate.save(tmp_path)
            return self._mp3_to_wav(Path(tmp_path))
        except Exception as e:
            raise EngineUnavailableError(f"Error en EdgeTTS: {e}") from e
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def _mp3_to_wav(self, mp3_path: Path) -> bytes:
        """Convert MP3 to WAV using pydub."""
        from pydub import AudioSegment

        audio = AudioSegment.from_mp3(str(mp3_path))
        audio = audio.set_frame_rate(24000).set_channels(1).set_sample_width(2)
        buffer = io.BytesIO()
        audio.export(buffer, format="wav")
        return buffer.getvalue()

    @staticmethod
    def _speed_to_rate(speed: float) -> str:
        """Convert speed multiplier to EdgeTTS rate string (e.g. '+0%', '+20%', '-10%')."""
        pct = int((speed - 1.0) * 100)
        if pct >= 0:
            return f"+{pct}%"
        return f"{pct}%"
