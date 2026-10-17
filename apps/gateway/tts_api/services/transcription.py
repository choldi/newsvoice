"""Auto-transcription of reference audio using faster-whisper (optional)."""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from tts_api.settings import get_settings

_model = None
_model_loaded = False


def _get_model():
    """Lazy-load the whisper model."""
    global _model, _model_loaded
    if _model_loaded:
        return _model
    try:
        from faster_whisper import WhisperModel

        settings = get_settings()
        logger.info("Loading faster-whisper model '%s'...", settings.TRANSCRIBE_MODEL)
        _model = WhisperModel(settings.TRANSCRIBE_MODEL, device="cpu", compute_type="int8")
        _model_loaded = True
        logger.info("faster-whisper model loaded")
    except ImportError:
        logger.warning("faster-whisper not installed. Auto-transcription disabled.")
        _model = None
        _model_loaded = True
    except Exception as e:
        logger.error("Failed to load faster-whisper model: %s", e)
        _model = None
        _model_loaded = True
    return _model


def is_available() -> bool:
    """Check if transcription is available."""
    settings = get_settings()
    if not settings.AUTO_TRANSCRIBE:
        return False
    return _get_model() is not None


def transcribe(audio_path: Path | str, language: str | None = None) -> str | None:
    """Transcribe an audio file.

    Args:
        audio_path: Path to the audio file
        language: Language hint (ISO 639-1, e.g. 'es', 'en', 'fr')

    Returns:
        Transcribed text, or None if unavailable.
    """
    model = _get_model()
    if model is None:
        return None

    try:
        # Map ISO 639-1 to whisper language codes (they match)
        segments, _info = model.transcribe(
            str(audio_path),
            language=language,
            beam_size=5,
            vad_filter=True,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        if text:
            logger.info("Transcribed reference audio: '%s'", text[:100])
            return text
        return None
    except Exception as e:
        logger.warning("Transcription failed: %s", e)
        return None
