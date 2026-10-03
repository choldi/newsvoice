"""Language-teacher routes (Phase 7.12): submit a class and poll its status.

Submitting stores the notes on the shared ``./data`` volume, records the job
and starts ``LessonWorkflow`` on the teacher queue. The LLM call and the
render both run in the worker; this file never touches either.

Both endpoints live under ``/api/admin/`` so the admin middleware guards them.
"""

from __future__ import annotations

import re
from pathlib import Path

import structlog
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from pydantic import ValidationError

from src.api.db.database import AudioJobsRepository
from src.api.models.lesson import LessonJobOut, LessonMode, LessonSubmitOut
from src.api.models.narration import NarrationSpec, SpeakerRole
from src.api.services.audio_files import atomic_write
from src.api.services.lesson import (
    LessonError,
    lesson_audio_link,
    lesson_upload_path,
    read_lesson_meta,
    update_lesson_meta,
    validate_lesson_id,
    validate_mode,
    validate_notes,
)
from src.api.services.signed_links import SignedLinkError
from src.api.services.temporal_admin import TemporalAdminService, new_lesson_run_id
from src.core.config import settings

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["lessons"])

LESSON_JOB_KIND = "lesson"
_LANGUAGE_RE = re.compile(r"[a-z]{2}")


def _language(value: str, field_name: str) -> str:
    """Validate a two-letter ISO 639-1 language code from a form field.

    Args:
        value: Raw form value.
        field_name: Field name used in the error message.

    Returns:
        The trimmed, lowercase code.

    Raises:
        HTTPException: 422 when the code is not ISO 639-1.
    """
    cleaned = (value or "").strip().lower()
    if not _LANGUAGE_RE.fullmatch(cleaned):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name} must be a two-letter ISO 639-1 code (e.g. 'es')",
        )
    return cleaned


def _cast_from_form(raw_spec: str | None, voice: str | None, target_language: str) -> NarrationSpec:
    """Resolve the cast that will voice the class.

    The spec (roles with their cloned voice ids and languages) is the
    operator's, as chosen for Phase 7.12: either pasted as a JSON field or
    built from the single ``voice`` field for one-voice modes.

    Args:
        raw_spec: Optional NarrationSpec JSON from the form.
        voice: Optional single voice id for a default one-role cast.
        target_language: Language used as the spec default.

    Returns:
        The validated spec.

    Raises:
        HTTPException: 422 when the spec JSON or the voice is unusable.
    """
    if raw_spec and raw_spec.strip():
        try:
            return NarrationSpec.model_validate_json(raw_spec.strip())
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"invalid spec JSON: {exc.errors()[0].get('msg', str(exc))}",
            ) from exc
    voice_id = (voice or "").strip() or settings.tts_voice
    try:
        return NarrationSpec(
            language=target_language,
            roles=[SpeakerRole(name="teacher", voice_id=voice_id)],
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid default cast: {exc.errors()[0].get('msg', str(exc))}",
        ) from exc


async def _read_upload(upload: UploadFile) -> bytes:
    """Read the notes bounded by ``teacher_max_file_bytes``.

    Args:
        upload: The multipart file part.

    Returns:
        The raw bytes.

    Raises:
        HTTPException: 413 when too large, 422 when empty.
    """
    limit = settings.teacher_max_file_bytes
    data = await upload.read(limit + 1)
    if not data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="uploaded file is empty",
        )
    if len(data) > limit:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file larger than {limit} bytes (teacher_max_file_bytes)",
        )
    return data


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
async def submit_lesson(
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
        voice: Optional single voice for a default cast.

    Returns:
        LessonSubmitOut with the workflow id and the status URL.

    Raises:
        HTTPException: 403 disabled, 413 too large, 415 unsupported type, 422
            invalid input, 502 Temporal failure, 503 no link secret.
    """
    if not settings.teacher_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Lessons are disabled (TEACHER_ENABLED=false)",
        )
    if not settings.doc_tts_link_secret.strip():
        # A lesson longer than the audio limit must ship a signed URL: without
        # a secret the result would be unusable, so refuse it up front.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DOC_TTS_LINK_SECRET is not configured; signed audio links are off",
        )
    declared = request.headers.get("content-length", "")
    overhead = 1_000_000
    if declared.isdigit() and int(declared) > settings.teacher_max_file_bytes + overhead:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"request larger than {settings.teacher_max_file_bytes} bytes",
        )

    filename = Path(file.filename or "").name.strip()
    if not filename:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="file part has no filename",
        )
    target = _language(target_language, "target_language")
    native = _language(native_language, "native_language")
    try:
        lesson_mode = LessonMode((mode or "mixed").strip().lower())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"mode must be one of {[m.value for m in LessonMode]}: {mode!r}",
        ) from exc

    data = await _read_upload(file)
    try:
        notes = validate_notes(data, filename)
    except LessonError as exc:
        detail = str(exc)
        if "unsupported file type" in detail:
            code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
        elif "too long" in detail:
            code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        else:
            code = status.HTTP_422_UNPROCESSABLE_ENTITY
        raise HTTPException(status_code=code, detail=detail) from exc

    cast = _cast_from_form(spec, voice, target)
    try:
        validate_mode(lesson_mode, cast)
    except LessonError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    workflow_id = new_lesson_run_id()
    upload_path = lesson_upload_path(workflow_id, filename)
    atomic_write(upload_path, data)
    clean_title = (title or "").strip() or Path(filename).stem
    collection_name = (collection or "").strip() or "default"
    channels, chat_id, email_recipients = _collection_delivery(collection_name)
    update_lesson_meta(
        workflow_id,
        {
            "title": clean_title,
            "filename": filename,
            "mode": lesson_mode.value,
            "target_language": target,
            "native_language": native,
            "collection": collection_name,
            "notes_chars": notes.char_count,
        },
    )

    repo = AudioJobsRepository()
    await repo.start_job(workflow_id, collection=collection_name, kind=LESSON_JOB_KIND)
    try:
        from src.workers.lesson_workflows import LessonConfig

        await TemporalAdminService().start_lesson(
            LessonConfig(
                notes_path=str(upload_path),
                filename=filename,
                mode=lesson_mode.value,
                spec=cast.model_dump(mode="json"),
                target_language=target,
                native_language=native,
                title=(title or "").strip(),
                collection=collection_name,
                delivery_task_queue=settings.temporal_task_queue,
                delivery_channels=channels,
                chat_id=chat_id,
                email_recipients=email_recipients,
            ),
            workflow_id,
        )
    except Exception as exc:
        logger.error("lesson_start_failed", workflow_id=workflow_id, error=str(exc), exc_info=True)
        await repo.finish_job(
            workflow_id,
            status="FINISHED_ERROR",
            error=f"could not start the workflow: {exc}",
            kind=LESSON_JOB_KIND,
        )
        upload_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not start the lesson workflow",
        ) from exc

    logger.info(
        "lesson_submitted",
        workflow_id=workflow_id,
        filename=filename,
        mode=lesson_mode.value,
        collection=collection_name,
        notes_chars=notes.char_count,
    )
    return LessonSubmitOut(
        status="ok",
        workflow_id=workflow_id,
        collection=collection_name,
        title=clean_title,
        mode=lesson_mode.value,
        status_url=f"/api/admin/lessons/{workflow_id}",
    )


def _collection_delivery(collection_name: str) -> tuple[list[str], str | None, list[str]]:
    """Resolve where a lesson is delivered for a collection (Phase 7.12).

    Unknown collections fall back to Telegram with the global chat id, which
    is what the digest pipeline does for the implicit default collection.

    Args:
        collection_name: Name of the configured collection.

    Returns:
        Tuple of (delivery_channels, chat_id, email_recipients).
    """
    from src.api.services.collections_repo import CollectionsRepository

    collection = CollectionsRepository().get(collection_name)
    if collection is None:
        return ["telegram"], None, []
    return (
        list(collection.delivery_channels) or ["telegram"],
        collection.chat_id,
        list(collection.email_recipients),
    )


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
    job = await AudioJobsRepository().get_job(workflow_id, kind=LESSON_JOB_KIND)
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
