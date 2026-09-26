"""Text-to-Speech route for generating MP3 audio from digest markdown."""

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
    status_code=status.HTTP_410_GONE,
    summary="Removed public TTS endpoint",
    description=(
        "Deprecated and disabled: the public synthesis endpoint was reachable "
        "without the admin token. Use POST /api/admin/tts instead."
    ),
)
async def generate_tts_removed() -> None:
    """Answer 410 Gone on the former public TTS path.

    Kept for one release so existing clients fail loudly instead of silently
    reading a 401. Remove in the next breaking release.

    Raises:
        HTTPException: Always 410 with the replacement path.
    """
    logger.info("tts_public_endpoint_removed", path="/api/generate_tts")
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Endpoint removed. Use POST /api/admin/tts (requires X-Admin-Token).",
    )


@router.post(
    "/admin/tts",
    response_model=TTSOut,
    status_code=status.HTTP_200_OK,
    summary="Generate MP3 audio from digest markdown (admin only)",
    description=(
        "Converts the digest markdown to an MP3 file using Edge neural voices "
        "(edge-tts). Caches the result by content hash in data/tts_cache/. "
        "Requires the admin token. Returns JSON with audio_path (not binary)."
    ),
)
async def generate_tts(payload: TTSIn) -> TTSOut:
    """POST /admin/tts endpoint handler.

    Args:
        payload: TTSIn model containing the markdown text and optional voice.

    Returns:
        TTSOut model with the audio file path and metadata.

    Raises:
        HTTPException: 503 if the TTS API is disabled, 504 on timeout,
            500 on any other failure.
    """
    if not settings.tts_api_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TTS API disabled (TTS_API_ENABLED=false)",
        )
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
