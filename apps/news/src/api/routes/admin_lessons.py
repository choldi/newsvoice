"""Language-teacher routes (Phase 7.12): submit a class and poll its status.

The submit rules live in ``src.api.services.lesson.submit_lesson`` so the GUI
(``/ui/tools``) applies exactly the same validation; this file only maps
:class:`SubmitError` onto HTTP codes and shapes the JSON responses.

Both endpoints live under ``/api/admin/`` so the admin middleware guards them.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status

from src.api.db.database import AudioJobsRepository
from src.api.models.lesson import LessonJobOut, LessonSubmitOut
from src.api.services.lesson import (
    JOB_KIND,
    LessonError,
    lesson_audio_link,
    read_lesson_meta,
    submit_lesson,
    validate_lesson_id,
)
from src.api.services.signed_links import SignedLinkError
from src.api.services.submit import SubmitError

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["lessons"])


@router.post(
    "/admin/lessons",
    response_model=LessonSubmitOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit notes for a language lesson (admin only)",
    description=(
        "Uploads .md/.txt notes, has the LLM build the class (readable Markdown "
        "+ spoken script) and renders it with the cast from 'spec'. Answers 202 "
        "with the workflow id; poll GET /api/admin/lessons/{workflow_id}. "
        "Requires the admin token."
    ),
)
async def submit_lesson_endpoint(
    request: Request,
    file: UploadFile = File(..., description="Notes in Markdown or plain text"),
    mode: str = Form(default="mixed", description="mixed | dual | native"),
    target_language: str = Form(..., description="ISO 639-1 language being taught"),
    native_language: str = Form(..., description="ISO 639-1 student language"),
    collection: str = Form(default="default", description="Collection owning the channels"),
    title: str = Form(default="", description="Lesson title (defaults to the filename)"),
    spec: str | None = Form(
        default=None, description="NarrationSpec JSON; the cast that voices the class"
    ),
    voice: str | None = Form(default=None, description="Single voice id when no spec is given"),
    voice2: str | None = Form(
        default=None,
        description="Second voice id (target language); required for mode='dual'",
    ),
) -> LessonSubmitOut:
    """POST /admin/lessons endpoint handler.

    Args:
        request: Incoming request (rejects oversized bodies early).
        file: The notes file.
        mode: Lesson mode.
        target_language: Language being taught.
        native_language: Student's native language.
        collection: Collection whose channels receive the lesson.
        title: Optional lesson title.
        spec: Optional NarrationSpec JSON.
        voice: Voice of the native-language role (the single role otherwise).
        voice2: Voice of the target-language role; required for mode='dual'.

    Returns:
        LessonSubmitOut with the workflow id and the status URL.

    Raises:
        HTTPException: Any failure the shared submit service reports (403,
            413, 415, 422, 502 or 503).
    """
    try:
        result = await submit_lesson(
            upload=file,
            target_language=target_language,
            native_language=native_language,
            content_length=request.headers.get("content-length", ""),
            mode=mode,
            collection=collection,
            title=title,
            spec=spec,
            voice=voice,
            voice2=voice2,
        )
    except SubmitError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return LessonSubmitOut(status="ok", **result)


@router.get(
    "/admin/lessons/{workflow_id}",
    response_model=LessonJobOut,
    summary="Poll a lesson job (admin only)",
    description=(
        "Returns the job status (STARTING, GENERATING, FINISHED_OK, "
        "FINISHED_ERROR) plus the lesson metadata and the HMAC-signed link to "
        "the audio once it exists. Requires the admin token."
    ),
)
async def get_lesson(workflow_id: str) -> LessonJobOut:
    """GET /admin/lessons/{workflow_id} endpoint handler.

    Args:
        workflow_id: Workflow id returned by the submit endpoint.

    Returns:
        LessonJobOut with the current status and the signed link when ready.

    Raises:
        HTTPException: 404 when the id is foreign or the job does not exist.
    """
    try:
        validate_lesson_id(workflow_id)
    except LessonError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="lesson not found"
        ) from exc
    job = await AudioJobsRepository().get_job(workflow_id, kind=JOB_KIND)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="lesson not found")
    meta = read_lesson_meta(workflow_id)
    audio_url: str | None = None
    if job["status"] == "FINISHED_OK" and job.get("audio_path"):
        try:
            audio_url = lesson_audio_link(workflow_id)
        except SignedLinkError as exc:
            logger.warning("lesson_link_unavailable", workflow_id=workflow_id, error=str(exc))
    duration = meta.get("duration_seconds")
    return LessonJobOut(
        status="ok",
        workflow_id=workflow_id,
        job_status=str(job["status"]),
        title=str(meta.get("title") or ""),
        mode=str(meta.get("mode") or ""),
        target_language=str(meta.get("target_language") or ""),
        native_language=str(meta.get("native_language") or ""),
        char_count=int(meta.get("char_count") or 0),
        duration_estimate=float(duration) if isinstance(duration, (int, float)) else None,
        audio_url=audio_url,
        text_path=str(meta.get("text_path") or ""),
        error=job.get("error"),
        elapsed_seconds=job.get("elapsed_seconds"),
    )
