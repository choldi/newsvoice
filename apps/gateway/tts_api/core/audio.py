"""Audio validation and processing for voice cloning."""

from __future__ import annotations

import tempfile
from pathlib import Path

import soundfile as sf
from loguru import logger

from tts_api.core import ffmpeg
from tts_api.core.exceptions import InvalidReferenceAudioError
from tts_api.settings import get_settings

# Sample rates required by each engine for reference audio
ENGINE_SAMPLE_RATES: dict[str, int] = {
    "pocket_tts": 24000,
    "edgetts": 24000,
    "omnivoice": 22050,
    "mock": 22050,
}


class AudioValidator:
    """Validates and processes reference audio for voice cloning."""

    def __init__(self, engine_name: str | None = None):
        self._settings = get_settings()
        if engine_name is None:
            engine_name = self._settings.TTS_ENGINE
            # For routed engines, default to pocket_tts sample rate
            if engine_name == "routed":
                engine_name = "pocket_tts"
        self._engine_name = engine_name
        self._target_sample_rate = ENGINE_SAMPLE_RATES.get(engine_name, 22050)
        self._target_channels = 1

    @property
    def target_sample_rate(self) -> int:
        return self._target_sample_rate

    async def validate_and_prepare(
        self,
        audio_path: Path | str,
        _language: str,
    ) -> tuple[Path, float]:
        """Validate and convert reference audio to required format.

        Args:
            audio_path: Path to the input audio file
            language: Language code (ISO 639-1)

        Returns:
            tuple: (path_to_processed_audio, duration_in_seconds)

        Raises:
            InvalidReferenceAudioError: If audio is invalid or cannot be processed
        """
        audio_path = Path(audio_path)

        if not audio_path.exists():
            raise InvalidReferenceAudioError(f"Audio file not found: {audio_path}")

        # Check file size
        file_size_mb = audio_path.stat().st_size / (1024 * 1024)
        if file_size_mb > self._settings.MAX_UPLOAD_SIZE_MB:
            raise InvalidReferenceAudioError(
                f"Audio file too large: {file_size_mb:.1f}MB > "
                f"{self._settings.MAX_UPLOAD_SIZE_MB}MB"
            )

        try:
            # Read audio info
            info = sf.info(audio_path)

            # Validate duration
            if info.duration < 1.0:
                raise InvalidReferenceAudioError(
                    f"Audio too short: {info.duration:.1f}s (minimum 1.0s)"
                )

            if info.duration > self._settings.MAX_REFERENCE_DURATION_SEC:
                raise InvalidReferenceAudioError(
                    f"Audio too long: {info.duration:.1f}s "
                    f"(maximum {self._settings.MAX_REFERENCE_DURATION_SEC}s)"
                )

            # Check sample rate and channels
            needs_conversion = (
                info.samplerate != self._target_sample_rate
                or info.channels != self._target_channels
            )

            if not needs_conversion:
                # Already in correct format
                return audio_path, info.duration

            # Convert audio
            logger.info(
                f"Converting audio: {info.samplerate}Hz {info.channels}ch -> "
                f"{self._target_sample_rate}Hz {self._target_channels}ch"
            )

            # Read and resample
            audio_data, _ = sf.read(audio_path, dtype="float32")

            # Convert to mono if needed
            if info.channels > 1:
                audio_data = audio_data.mean(axis=1)

            # Resample if needed
            if info.samplerate != self._target_sample_rate:
                resampled = ffmpeg.resample(audio_data, info.samplerate, self._target_sample_rate)
                if resampled is None:
                    raise InvalidReferenceAudioError(
                        "No se puede remuestrear la referencia: falta el binario 'ffmpeg'"
                    )
                audio_data = resampled

            # Write to temporary file
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = Path(tmp.name)
                sf.write(tmp_path, audio_data, self._target_sample_rate, subtype="PCM_16")

            # Verify output
            out_info = sf.info(tmp_path)
            return tmp_path, out_info.duration

        except InvalidReferenceAudioError:
            raise
        except Exception as e:
            raise InvalidReferenceAudioError(f"Failed to process audio: {e}") from e
