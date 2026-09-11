"""Event clustering route for grouping articles by story."""

import asyncio

import structlog
from fastapi import APIRouter, HTTPException, status

from src.api.models.cluster import ClusterIn, ClusterOut
from src.api.services.cluster import ClusterService
from src.core.config import settings

logger = structlog.get_logger()

router = APIRouter(tags=["cluster"])
cluster_service = ClusterService()


@router.post(
    "/cluster",
    response_model=ClusterOut,
    status_code=status.HTTP_200_OK,
    summary="Cluster articles by event fingerprint",
    description=(
        "Groups articles by {entity, action, title_hash} fingerprint. "
        "Detects material updates for existing clusters. "
        "Returns clusters ready for digest generation."
    ),
)
async def cluster_articles(payload: ClusterIn) -> ClusterOut:
    """POST /cluster endpoint handler.

    Args:
        payload: ClusterIn model containing deduplicated articles.

    Returns:
        ClusterOut model containing event clusters with material update flags.

    Raises:
        HTTPException: If clustering fails.
    """
    try:
        clusters = await asyncio.wait_for(
            cluster_service.cluster_articles(payload),
            timeout=settings.api_request_timeout,
        )
        new_clusters = sum(1 for c in clusters if c.is_material_update and len(c.articles) == 1)
        updated_clusters = sum(1 for c in clusters if c.is_material_update and len(c.articles) > 1)
        return ClusterOut(
            status="ok",
            clusters=clusters,
            total_articles=len(payload.articles),
            new_clusters=new_clusters,
            updated_clusters=updated_clusters,
        )
    except TimeoutError as exc:
        logger.error("cluster_articles_timeout")
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Request timed out",
        ) from exc
    except Exception as exc:
        logger.error("cluster_articles_failed", error=str(exc), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from exc
