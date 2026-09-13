"""Admin runs routes for the Phase 6 GUI.

Serve the digest_runs history (list + detail) and let the operator trigger a
collection immediately.
"""

import structlog
from fastapi import APIRouter, HTTPException, Query, status

from src.api.db.database import RunsRepository
from src.api.models.admin import RunOut, RunsListOut, TriggerRunOut
from src.api.services.collections_repo import CollectionsRepository
from src.api.services.temporal_admin import TemporalAdminService

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["admin-runs"])


@router.get("/admin/runs", response_model=RunsListOut)
async def list_runs(
    collection: str | None = Query(default=None, description="Filter by collection name"),
    run_status: str | None = Query(default=None, alias="status", description="Filter by status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> RunsListOut:
    """List recent digest runs, newest first.

    Args:
        collection: Optional collection name filter.
        run_status: Optional run status filter ('ok', 'no_new_articles', 'failed').
        limit: Maximum rows to return.
        offset: Pagination offset.

    Returns:
        RunsListOut with the matching runs and the unfiltered-paginated total.
    """
    repo = RunsRepository()
    runs = await repo.list_runs(
        collection=collection, status_filter=run_status, limit=limit, offset=offset
    )
    total = await repo.count(collection=collection, status_filter=run_status)
    return RunsListOut(runs=runs, total=total)


@router.get("/admin/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: int) -> RunOut:
    """Return a single digest run by id.

    Args:
        run_id: Digest run row id.

    Returns:
        RunOut with the run details.

    Raises:
        HTTPException: 404 if the run does not exist.
    """
    run = await RunsRepository().get_run(run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )
    return RunOut(run=run)


@router.post("/admin/collections/{name}/run", response_model=TriggerRunOut)
async def trigger_run(name: str) -> TriggerRunOut:
    """Run a collection's digest pipeline immediately.

    Args:
        name: Collection name.

    Returns:
        TriggerRunOut with the started workflow id.

    Raises:
        HTTPException: 404 if the collection does not exist, 502 if the run fails to start.
    """
    repo = CollectionsRepository()
    collection = repo.get(name)
    if collection is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Collection '{name}' not found",
        )
    try:
        service = TemporalAdminService()
        workflow_id = await service.trigger_collection(collection)
    except Exception as exc:
        logger.error("trigger_run_failed", collection=name, error=str(exc), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to start pipeline: {exc}",
        ) from exc
    return TriggerRunOut(workflow_id=workflow_id, collection=collection.name)
