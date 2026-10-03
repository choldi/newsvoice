"""Single-URL summary route (Phase 7.10): summarize one article by URL.

The request only starts a Temporal workflow: the page is downloaded, the body
is extracted with trafilatura, the digest is generated with the collection's
prompt and then delivered to the collection's configured channels. The endpoint
answers 202 with the workflow id, and the outcome lands in `digest_runs` (GUI
`/ui`) like any other run.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, status

from src.api.models.summarize_url import UrlSummarizeIn, UrlSummarizeOut
from src.api.services.collections_repo import CollectionsRepository
from src.api.services.temporal_admin import TemporalAdminService
from src.core.collections import default_collection
from src.core.config import settings

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["summarize"])


def _resolve_collection(name: str) -> object:
    """Return the collection the run must use, or raise a 404.

    The implicit "default" collection falls back to the global settings so the
    endpoint also works on an install without collections.toml.

    Args:
        name: Collection name from the request.

    Returns:
        The resolved CollectionConfig.

    Raises:
        HTTPException: 404 when the named collection does not exist.
    """
    collection = CollectionsRepository().get(name)
    if collection is None and name.casefold() == "default".casefold():
        collection = default_collection()
    if collection is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Collection '{name}' not found",
        )
    return collection


@router.post(
    "/summarize/url",
    response_model=UrlSummarizeOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Summarize a single article URL",
    description=(
        "Downloads the article, extracts its readable body with trafilatura, "
        "generates a digest with the collection's prompt and delivers it to the "
        "collection's channels. Answers 202 with the Temporal workflow id; the "
        "outcome is recorded in the runs table and shown in /ui."
    ),
)
async def summarize_url(payload: UrlSummarizeIn) -> UrlSummarizeOut:
    """POST /summarize/url endpoint handler.

    Args:
        payload: UrlSummarizeIn with the article URL and options.

    Returns:
        UrlSummarizeOut with the workflow id of the started run.

    Raises:
        HTTPException: 403 when the endpoint is disabled, 404 for an unknown
            collection, 502 when Temporal cannot start the workflow.
    """
    if not settings.url_summary_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="URL summary is disabled (url_summary_enabled=false)",
        )
    collection = _resolve_collection(payload.collection)
    try:
        workflow_id = await TemporalAdminService().start_url_summarize(
            collection,
            url=payload.url,
            store=payload.store,
            deliver=payload.deliver,
        )
    except Exception as exc:
        logger.error("url_summary_start_failed", url=payload.url, error=str(exc), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not start the URL summary workflow",
        ) from exc
    logger.info(
        "url_summary_started",
        workflow_id=workflow_id,
        collection=collection.name,
        url=payload.url,
        store=payload.store,
        deliver=payload.deliver,
    )
    return UrlSummarizeOut(
        status="ok",
        workflow_id=workflow_id,
        collection=collection.name,
        url=payload.url,
    )
