"""Document TTS routes (Phase 7.11): submit a document and poll its job.

The submit rules live in ``src.api.services.doc_tts.submit_document`` so the
GUI (``/ui/tools``) applies exactly the same validation; this file only maps
:class:`SubmitError` onto HTTP codes and shapes the JSON responses.

Both endpoints live under ``/api/admin/`` so the admin middleware guards them.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status

from src.api.db.database import AudioJobsRepository
from src.api.models.doc_tts import DocTTSJobOut, DocTTSSubmitOut
from src.api.services.doc_tts import (
    JOB_KIND,
    read_job_meta,
    submit_document,
    validate_job_id,
)
from src.api.services.signed_links import SignedLinkError, build_signed_path
from src.api.services.submit import SubmitError
from src.api.services.tts_estimate import eta_seconds

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["documents"])


@router.post(
    "/admin/documents/tts",
    response_model=DocTTSSubmitOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a document for TTS (admin only)",
    description=(
        "Uploads a PDF, EPUB, TXT or Markdown file, records the job and starts "
        "DocumentTtsWorkflow on the dedicated doc-tts queue. Answers 202 with "
        "the workflow id; poll GET /api/admin/documents/tts/{workflow_id} for "
        "the status and the signed audio link. Requires the admin token."
    ),
)
async def submit_document_tts(
    request: Request,
    file: UploadFile = File(..., description="PDF, EPUB, TXT or MD file"),
    voice: str | None = Form(default=None, description="Edge voice override"),
    language: str | None = Form(default=None, description="ISO 639-1 language override"),
    title: str | None = Form(default=None, description="Title for the audio file"),
) -> DocTTSSubmitOut:
    """POST /admin/documents/tts endpoint handler.

    Args:
        request: Incoming request (used to reject oversized bodies early).
        file: The document to synthesize.
        voice: Optional voice override.
        language: Optional language override.
        title: Optional title override.

    Returns:
        DocTTSSubmitOut with the workflow id and the status URL.

    Raises:
        HTTPException: Any failure the shared submit service reports (403,
            413, 415, 422, 502 or 503).
    """
    try:
        result = await submit_document(
            upload=file,
            content_length=request.headers.get("content-length", ""),
            voice=voice,
            language=language,
            title=title,
        )
    except SubmitError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return DocTTSSubmitOut(
        status="ok",
        workflow_id=result["workflow_id"],
        title=result["title"],
        filename=result["filename"],
        status_url=result["status_url"],
    )


@router.get(
    "/admin/documents/tts/{workflow_id}",
    response_model=DocTTSJobOut,
    summary="Poll a document TTS job (admin only)",
    description=(
        "Returns the job status (STARTING, GENERATING, FINISHED_OK, "
        "FINISHED_ERROR) plus the document metadata and, once finished, the "
        "HMAC-signed link to the MP3. Requires the admin token."
    ),
)
async def get_document_tts(workflow_id: str) -> DocTTSJobOut:
    """GET /admin/documents/tts/{workflow_id} endpoint handler.

    Args:
        workflow_id: Workflow id returned by the submit endpoint.

    Returns:
        DocTTSJobOut with the current status and the signed link when ready.

    Raises:
        HTTPException: 404 when the id is foreign or the job does not exist.
    """
    try:
        validate_job_id(workflow_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="document job not found"
        ) from exc
    job = await AudioJobsRepository().get_job(workflow_id, kind=JOB_KIND)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document job not found")
    meta = read_job_meta(workflow_id)
    estimate = meta.get("estimate") if isinstance(meta.get("estimate"), dict) else {}
    progress = meta.get("progress") if isinstance(meta.get("progress"), dict) else {}
    audio_url: str | None = None
    if job["status"] == "FINISHED_OK" and job.get("audio_path"):
        try:
            audio_url = build_signed_path(workflow_id)
        except SignedLinkError as exc:
            # The job succeeded but links are unusable (secret removed after
            # the run): report the status and leave the URL empty.
            logger.warning("doc_tts_link_unavailable", workflow_id=workflow_id, error=str(exc))
    return DocTTSJobOut(
        status="ok",
        workflow_id=workflow_id,
        job_status=str(job["status"]),
        title=str(meta.get("title") or ""),
        filename=str(meta.get("filename") or ""),
        kind=str(meta.get("kind") or ""),
        char_count=int(meta.get("char_count") or 0),
        voice=str(meta.get("voice") or ""),
        language=str(meta.get("language") or ""),
        duration_estimate=meta.get("duration_estimate"),
        estimate_wall_seconds=estimate.get("wall_seconds"),
        eta_seconds=eta_seconds(estimate, progress),
        chunks_done=int(progress.get("done") or 0),
        chunks_total=int(progress.get("total") or 0),
        paused=job["status"] == "PAUSED",
        chunk_count=int(meta.get("chunk_count") or 0),
        audio_url=audio_url,
        error=job.get("error"),
        elapsed_seconds=job.get("elapsed_seconds"),
    )
