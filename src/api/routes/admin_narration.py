"""Admin narration routes for the Phase 7.4 GUI.

Provides endpoints to manually render a narration and to list available voices
for the cast editor.
"""

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from src.api.db.database import AudioJobsRepository, NarrationRepository, RunsRepository
from src.api.models.narration import NarrationScript, NarrationSpec
from src.api.services.narration_tts import NarrationTTSService
from src.core.config import settings
from src.workers.narration_workflows import NarrationConfig, NarrationWorkflow
from src.workers.podcast_target import podcast_target
from src.workers.run_worker import build_client

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["admin-narration"])


@router.get("/admin/narration/voices", response_model=dict)
async def list_narration_voices(language: str | None = None) -> dict:
    """List available voices for the narration cast editor.

    Args:
        language: Optional ISO 639-1 language filter (e.g. 'es').

    Returns:
        Dict with 'voices' list containing voice id, name, language, and gender.
    """
    service = NarrationTTSService()
    voices = await service.list_voices(language=language)
    return {"voices": voices}


class NarrationTriggerIn(BaseModel):
    """Input for manually triggering a narration render."""

    collection: str = Field(default="default", description="Collection name")
    spec: NarrationSpec = Field(..., description="Narration spec (cast, music, defaults)")
    script: NarrationScript | None = Field(default=None, description="Inline narration script")
    script_id: int | None = Field(
        default=None, description="Id of a stored script in narration_scripts"
    )
    strategy: str = Field(
        default="turns", description="Render strategy: 'turns' or 'conversations'"
    )
    run_id: str = Field(
        default="", description="Existing digest workflow id to attach the narration to"
    )
    force: bool = Field(default=False, description="Run even when NARRATION_ENABLED is false")


class NarrationTriggerOut(BaseModel):
    """Output for a triggered narration render."""

    workflow_id: str
    collection: str
    status: str


@router.post("/admin/narration", response_model=NarrationTriggerOut)
async def trigger_narration(payload: NarrationTriggerIn) -> NarrationTriggerOut:
    """Start a narration workflow manually.

    Args:
        payload: NarrationTriggerIn with spec, optional script, and options.

    Returns:
        NarrationTriggerOut with the started workflow id.

    Raises:
        HTTPException: 404 if collection not found, 422 if neither script nor script_id provided,
            502 if the workflow fails to start.
    """
    if not settings.narration_enabled and not payload.force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Narration is disabled (NARRATION_ENABLED=false). Use force=true to render anyway."
            ),
        )

    if not payload.script and payload.script_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Either script or script_id must be provided.",
        )

    if payload.script_id is not None:
        repo = NarrationRepository()
        stored = await repo.get_script(payload.script_id)
        if not stored:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Script {payload.script_id} not found",
            )
        script = stored
    else:
        script = payload.script

    config = NarrationConfig(
        collection=payload.collection,
        spec=payload.spec.model_dump(),
        script=script.model_dump() if script else None,
        script_id=payload.script_id,
        run_id=payload.run_id,
        strategy=payload.strategy,
    )
    workflow_id = f"narration-{payload.collection}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    # Phase 7.9b: podcast generation owns one namespace per backend, so a
    # standalone render is started in the namespace the spec asks for.
    target = podcast_target(payload.spec.model_dump())
    client = await build_client(target.namespace)
    try:
        await client.execute_workflow(
            NarrationWorkflow.run,
            config,
            id=workflow_id,
            task_queue=target.task_queue,
        )
    except Exception as exc:
        logger.error(
            "trigger_narration_failed",
            collection=payload.collection,
            error=str(exc),
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to start narration: {exc}",
        ) from exc

    return NarrationTriggerOut(
        workflow_id=workflow_id, collection=payload.collection, status="started"
    )


class NarrationScriptSaveIn(BaseModel):
    """Input for saving a narration script."""

    run_id: str = Field(..., description="Workflow id (or narration id) the script belongs to")
    script: NarrationScript = Field(..., description="Validated script model to persist")
    collection: str = Field(default="default", description="Collection name")


class NarrationScriptSaveOut(BaseModel):
    """Output for a saved narration script."""

    id: int
    run_id: str
    collection: str


@router.post("/admin/narration/script", response_model=NarrationScriptSaveOut)
async def save_narration_script(payload: NarrationScriptSaveIn) -> NarrationScriptSaveOut:
    """Save a narration script for later rendering.

    Args:
        payload: NarrationScriptSaveIn with run_id, script, and collection.

    Returns:
        NarrationScriptSaveOut with the saved script id.
    """
    from src.api.db.database import NarrationRepository

    repo = NarrationRepository()
    script_id = await repo.save_script(payload.run_id, payload.script, payload.collection)
    return NarrationScriptSaveOut(
        id=script_id, run_id=payload.run_id, collection=payload.collection
    )


@router.get("/admin/narration/runs/{run_id}/audio", response_model=dict)
async def get_narration_audio(run_id: int) -> dict:
    """Return the narration generation status for a digest run.

    Args:
        run_id: Digest run row id.

    Returns:
        Dict with the audio_jobs row for the narration kind.

    Raises:
        HTTPException: 404 if the run or its narration job does not exist.
    """
    run = await RunsRepository().get_run(run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )
    job = await AudioJobsRepository().get_job(run["workflow_id"], kind="narration")
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No narration audio job recorded for run {run_id}",
        )
    return {"job": job}


@router.get("/admin/narration/scripts/{script_id}", response_model=dict)
async def get_narration_script(script_id: int) -> dict:
    """Return a stored narration script by id.

    Args:
        script_id: Id of the narration_scripts row.

    Returns:
        Dict with the parsed NarrationScript.

    Raises:
        HTTPException: 404 if the script does not exist.
    """
    from src.api.db.database import NarrationRepository

    repo = NarrationRepository()
    script = await repo.get_script(script_id)
    if script is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Script {script_id} not found",
        )
    return {"script": script.model_dump()}
