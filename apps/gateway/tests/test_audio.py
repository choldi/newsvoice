"""Tests unitarios para el validador de audio."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import soundfile as sf

from tts_api.core import ffmpeg
from tts_api.core.audio import AudioValidator
from tts_api.core.exceptions import InvalidReferenceAudioError


@pytest.fixture
def validator() -> AudioValidator:
    """Validador de audio."""
    with patch("tts_api.core.audio.get_settings") as mock_get_settings:
        settings_mock = MagicMock()
        settings_mock.MAX_UPLOAD_SIZE_MB = 10
        settings_mock.MAX_REFERENCE_DURATION_SEC = 30.0
        mock_get_settings.return_value = settings_mock
        yield AudioValidator()


def _create_wav(
    path: Path, duration: float = 2.0, sample_rate: int = 22050, channels: int = 1
) -> None:
    """Crea un archivo WAV para tests."""
    num_samples = int(duration * sample_rate)
    data = np.zeros(num_samples, dtype=np.float32)
    if channels > 1:
        data = np.zeros((num_samples, channels), dtype=np.float32)
    sf.write(str(path), data, sample_rate)


@pytest.mark.asyncio
async def test_validate_valid_audio(validator: AudioValidator, tmp_path: Path) -> None:
    """Test de validación de audio válido."""
    wav_path = tmp_path / "test.wav"
    _create_wav(wav_path)
    processed_path, duration_sec = await validator.validate_and_prepare(wav_path, "es")
    assert duration_sec == pytest.approx(2.0, abs=0.1)
    info = sf.info(processed_path)
    assert info.samplerate == 22050
    assert info.channels == 1


@pytest.mark.asyncio
async def test_validate_file_not_found(validator: AudioValidator) -> None:
    """Test de error cuando el archivo no existe."""
    with pytest.raises(InvalidReferenceAudioError, match="not found"):
        await validator.validate_and_prepare("/nonexistent/audio.wav", "es")


@pytest.mark.asyncio
async def test_validate_audio_too_short(validator: AudioValidator, tmp_path: Path) -> None:
    """Test de error cuando el audio es muy corto."""
    wav_path = tmp_path / "short.wav"
    _create_wav(wav_path, duration=0.1)
    with pytest.raises(InvalidReferenceAudioError, match="too short"):
        await validator.validate_and_prepare(wav_path, "es")


@pytest.mark.asyncio
async def test_validate_audio_too_long(validator: AudioValidator, tmp_path: Path) -> None:
    """Test de error cuando el audio es muy largo."""
    wav_path = tmp_path / "long.wav"
    _create_wav(wav_path, duration=60.0)
    with pytest.raises(InvalidReferenceAudioError, match="too long"):
        await validator.validate_and_prepare(wav_path, "es")


@pytest.mark.asyncio
async def test_validate_smaller_sample_rate_is_resampled(
    validator: AudioValidator, tmp_path: Path
) -> None:
    """Test de que un sample rate menor se resamplea a 22050 (no se rechaza)."""
    wav_path = tmp_path / "lo_rate.wav"
    _create_wav(wav_path, sample_rate=4000)
    processed_path, _ = await validator.validate_and_prepare(wav_path, "es")
    assert sf.info(processed_path).samplerate == 22050


@pytest.mark.asyncio
async def test_validate_stereo_converts_to_mono(validator: AudioValidator, tmp_path: Path) -> None:
    """Test de conversión de estéreo a mono."""
    wav_path = tmp_path / "stereo.wav"
    _create_wav(wav_path, channels=2)
    processed_path, _ = await validator.validate_and_prepare(wav_path, "es")
    processed = sf.SoundFile(processed_path)
    assert processed.channels == 1
    processed.close()


@pytest.mark.asyncio
async def test_validate_resamples_different_rate(validator: AudioValidator, tmp_path: Path) -> None:
    """Test de resampling cuando el sample rate es diferente."""
    wav_path = tmp_path / "rate.wav"
    _create_wav(wav_path, sample_rate=44100)
    processed_path, _ = await validator.validate_and_prepare(wav_path, "es")
    processed = sf.SoundFile(processed_path)
    assert processed.samplerate == 22050
    processed.close()


@pytest.mark.asyncio
async def test_validate_resample_sin_ffmpeg_falla_explicito(
    validator: AudioValidator, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin el binario ffmpeg el resample falla con error de dominio."""
    monkeypatch.setattr(ffmpeg, "resample", lambda *_args, **_kwargs: None)
    wav_path = tmp_path / "lo_rate.wav"
    _create_wav(wav_path, duration=1.0, sample_rate=4000)

    with pytest.raises(InvalidReferenceAudioError, match="ffmpeg"):
        await validator.validate_and_prepare(wav_path, "es")


@pytest.mark.asyncio
async def test_validate_invalid_audio_file(validator: AudioValidator, tmp_path: Path) -> None:
    """Test de error con archivo de audio inválido."""
    bad_file = tmp_path / "bad.wav"
    bad_file.write_text("not an audio file")
    with pytest.raises(InvalidReferenceAudioError):
        await validator.validate_and_prepare(bad_file, "es")


@pytest.mark.asyncio
async def test_validate_integer_audio_data(validator: AudioValidator, tmp_path: Path) -> None:
    """Test de procesamiento de audio con datos enteros (16-bit PCM)."""
    wav_path = tmp_path / "int16.wav"
    num_samples = int(2.0 * 22050)
    data = np.zeros(num_samples, dtype=np.int16)
    sf.write(str(wav_path), data, 22050, subtype="PCM_16")
    _, duration_sec = await validator.validate_and_prepare(wav_path, "es")
    assert duration_sec == pytest.approx(2.0, abs=0.1)
