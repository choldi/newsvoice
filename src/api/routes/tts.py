"""Text-to-Speech route for generating audio from digest markdown."""

import asyncio

import structlog
from fastapi import APIRouter, HTTPException, status

from src.api.models.narration import TTSBackend
from src.api.models.tts import TTSIn, TTSOut
from src.api.services.tts import TTSService
from src.api.services.tts_backends import TTSBackendError, resolve_backend
from src.core.config import settings

logger = structlog.get_logger(__name__)

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
    summary="Generate audio from digest markdown (admin only)",
    description=(
        "Converts the digest markdown with the requested engine (edge by "
        "default; the tts-api gateway or a Modal backend when asked) and caches "
        "the result by content hash in data/tts_cache/. Requires the admin "
        "token. Returns JSON with audio_path (not binary)."
    ),
)
async def generate_tts(payload: TTSIn) -> TTSOut:
    """POST /admin/tts endpoint handler.

    Args:
        payload: TTSIn model containing the markdown text and optional
            voice/language/backend.

    Returns:
        TTSOut model with the audio file path and metadata.

    Raises:
        HTTPException: 422 when the engine or voice cannot serve the request,
            503 if the TTS API is disabled, 504 on timeout, 500 on any other
            failure.
    """
    if not settings.tts_api_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TTS API disabled (TTS_API_ENABLED=false)",
        )
    try:
        engine = resolve_backend(payload.backend)
    except TTSBackendError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    # edge is a local call that fits the normal request budget; a gateway or
    # Modal render of a full digest cannot, so it gets its own ceiling instead
    # of timing out on every call.
    budget = (
        settings.api_request_timeout
        if engine is TTSBackend.EDGE
        else settings.tts_backend_timeout_seconds
    )
    try:
        service = TTSService()
        return await asyncio.wait_for(service.generate_tts(payload), timeout=budget)
    except TTSBackendError as exc:
        # A voice outside the chosen engine's catalogue is a request problem,
        # not a server fault.
        logger.warning("tts_backend_rejected", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
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
