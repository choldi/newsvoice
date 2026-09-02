"""Clustering route for grouping related news articles into event clusters."""

import structlog
from fastapi import APIRouter, HTTPException, status

from src.api.models.cluster import ClusterIn, ClusterOut
from src.api.services.cluster import ClusterService

logger = structlog.get_logger()

router = APIRouter(tags=["cluster"])
cluster_service = ClusterService()


@router.post(
    "/cluster",
    response_model=ClusterOut,
    status_code=status.HTTP_200_OK,
    summary="Group articles into event story clusters",
    description=(
        "Derives event fingerprints ({entity, action, normalized_title_hash}), "
        "groups related coverage of the same event into clusters, "
        "and checks for material updates against previously seen events."
    ),
)
async def cluster_articles(payload: ClusterIn) -> ClusterOut:
    """POST /cluster endpoint handler.

    Args:
        payload: ClusterIn model containing list of articles.

    Returns:
        ClusterOut model with grouped event clusters.

    Raises:
        HTTPException: If an error occurs during event clustering.
    """
    try:
        clusters = await cluster_service.cluster_articles(payload.articles)
        return ClusterOut(
            status="ok",
            clusters=clusters,
            total_articles=len(payload.articles),
            total_clusters=len(clusters),
        )
    except Exception as exc:
        logger.error("cluster_articles_endpoint_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to cluster articles: {str(exc)}",
        ) from exc
