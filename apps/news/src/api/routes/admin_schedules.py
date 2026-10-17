"""Admin schedules routes for the Phase 6 GUI.

Resync reconciles the per-collection Temporal schedules with the current
collections.toml configuration.
"""

import structlog
from fastapi import APIRouter, HTTPException, status

from src.api.models.admin import ResyncOut
from src.api.services.collections_repo import CollectionsRepository
from src.api.services.temporal_admin import TemporalAdminService

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["admin-schedules"])


@router.post("/admin/schedules/resync", response_model=ResyncOut)
async def resync_schedules() -> ResyncOut:
    """Reconcile Temporal schedules with the collections configuration.

    Creates or updates schedules for enabled collections and deletes schedules
    whose collection no longer exists or is disabled.

    Returns:
        ResyncOut with created/updated and removed schedule ids.

    Raises:
        HTTPException: 502 if the resync fails at the Temporal level.
    """
    try:
        service = TemporalAdminService()
        result = await service.resync_schedules(CollectionsRepository().list())
    except Exception as exc:
        logger.error("schedule_resync_failed", error=str(exc), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Schedule resync failed: {exc}",
        ) from exc
    return ResyncOut(**result)
