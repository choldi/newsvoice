"""Ingest and deduplication route for filtering unseen articles."""

import structlog
from fastapi import APIRouter, HTTPException, status

from src.api.models.article import FilterNewIn, FilterNewOut
from src.api.services.dedup import DedupService

logger = structlog.get_logger()

router = APIRouter(tags=["ingest"])
dedup_service = DedupService()


@router.post(
    "/filter_new",
    response_model=FilterNewOut,
    status_code=status.HTTP_200_OK,
    summary="Filter new and unseen RSS articles",
    description=(
        "Normalizes article URLs, removes marketing tracking params, "
        "and filters against SQLite history within a 7-day retention window."
    ),
)
async def filter_new_articles(payload: FilterNewIn) -> FilterNewOut:
    """POST /filter_new endpoint handler.

    Args:
        payload: FilterNewIn model containing raw list of RSS articles.

    Returns:
        FilterNewOut model containing filtered unseen articles.

    Raises:
        HTTPException: If an error occurs during article deduplication.
    """
    try:
        new_articles = await dedup_service.filter_new_articles(payload.articles)
        return FilterNewOut(
            status="ok",
            new_articles=new_articles,
            total_received=len(payload.articles),
            new_count=len(new_articles),
        )
    except Exception as exc:
        logger.error("filter_new_articles_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to filter new articles: {str(exc)}",
        ) from exc
