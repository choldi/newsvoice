"""MVP Digest route for Phase 1 single endpoint."""

import asyncio

import structlog
from fastapi import APIRouter, HTTPException, status

from src.api.models.article import DigestIn, DigestOut
from src.api.services.digest import DigestService
from src.core.config import settings

logger = structlog.get_logger()

router = APIRouter(tags=["mvp"])
digest_service = DigestService()


@router.post(
    "/mvp_digest",
    response_model=DigestOut,
    status_code=status.HTTP_200_OK,
    summary="Generate daily MVP news digest",
    description=(
        "Receives a list of articles and returns 5 main stories + 5 headlines in Markdown format."
    ),
)
async def generate_mvp_digest(payload: DigestIn) -> DigestOut:
    """POST /mvp_digest endpoint handler.

    Args:
        payload: DigestIn model containing article list.

    Returns:
        DigestOut model with generated summary.

    Raises:
        HTTPException: If an unhandled exception occurs during digest generation.
    """
    try:
        return await asyncio.wait_for(
            digest_service.generate_digest(payload),
            timeout=settings.api_request_timeout,
        )
    except TimeoutError as exc:
        logger.error("mvp_digest_timeout")
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Request timed out",
        ) from exc
    except Exception as exc:
        logger.error("mvp_digest_failed", error=str(exc), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from exc
