"""Text-to-Speech route for generating MP3 audio from digest markdown."""

import structlog
from fastapi import APIRouter, HTTPException, status

from src.api.models.tts import TTSIn, TTSOut
from src.api.services.tts import TTSService

logger = structlog.get_logger()

router = APIRouter(tags=["tts"])


@router.post(
    "/generate_tts",
    response_model=TTSOut,
    status_code=status.HTTP_200_OK,
    summary="Generate MP3 audio from digest markdown",
    description=(
        "Converts the digest markdown to an MP3 file using edge-tts. "
        "Caches the result by content hash in data/tts_cache/. "
        "Returns JSON with audio_path (not binary)."
    ),
)
async def generate_tts(payload: TTSIn) -> TTSOut:
    """POST /generate_tts endpoint handler.

    Args:
        payload: TTSIn model containing the markdown text and optional voice.

    Returns:
        TTSOut model with the audio file path and metadata.

    Raises:
        HTTPException: If TTS generation fails.
    """
    try:
        service = TTSService()
        return await service.generate_tts(payload)
    except Exception as exc:
        logger.error("tts_generate_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate TTS audio: {str(exc)}",
        ) from exc
