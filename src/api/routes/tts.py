"""Text-to-Speech route for generating WAV audio from digest markdown."""

import asyncio

import structlog
from fastapi import APIRouter, HTTPException, status

from src.api.models.tts import TTSIn, TTSOut
from src.api.services.tts import TTSService
from src.core.config import settings

logger = structlog.get_logger()

router = APIRouter(tags=["tts"])


@router.post(
    "/generate_tts",
    response_model=TTSOut,
    status_code=status.HTTP_200_OK,
    summary="Generate WAV audio from digest markdown",
    description=(
        "Converts the digest markdown to a WAV file using the OmniVoice API. "
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
        return await asyncio.wait_for(
            service.generate_tts(payload),
            timeout=settings.api_request_timeout,
        )
    except TimeoutError as exc:
        logger.error("tts_generate_timeout")
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Request timed out",
        ) from exc
    except Exception as exc:
        logger.error("tts_generate_failed", error=str(exc), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from exc
