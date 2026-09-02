"""Summarization route for daily news digest generation."""

import structlog
from fastapi import APIRouter, HTTPException, status

from src.api.models.article import DigestIn, DigestOut
from src.api.services.digest import DigestService

logger = structlog.get_logger()

router = APIRouter(tags=["summarize"])
digest_service = DigestService()


@router.post(
    "/summarize",
    response_model=DigestOut,
    status_code=status.HTTP_200_OK,
    summary="Generate daily news digest from articles",
    description=(
        "Receives a list of curated or filtered articles and generates "
        "a 5 main stories + 5 headlines Markdown digest via local LLM."
    ),
)
async def summarize_articles(payload: DigestIn) -> DigestOut:
    """POST /summarize endpoint handler.

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
        logger.error("summarize_articles_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate news digest: {str(exc)}",
        ) from exc
