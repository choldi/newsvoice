"""MVP Digest route for Phase 1 single endpoint."""

import structlog
from fastapi import APIRouter, HTTPException, status

from src.api.models.article import DigestIn, DigestOut
from src.api.services.digest import DigestService

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
        return await digest_service.generate_digest(payload)
    except Exception as exc:
        logger.error("mvp_digest_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate news digest: {str(exc)}",
        ) from exc
