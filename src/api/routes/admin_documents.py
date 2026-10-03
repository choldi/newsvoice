"""Document TTS routes (Phase 7.11): submit a document and poll its job.

Submitting stores the upload on the shared ``./data`` volume, records the job
and starts ``DocumentTtsWorkflow`` on the dedicated doc-tts queue. The
synthesis itself runs in the worker; this file never touches the TTS engine.

Both endpoints live under ``/api/admin/`` so the admin middleware guards them.
"""

from __future__ import annotations

from pathlib import Path

import structlog
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status

from src.api.db.database import AudioJobsRepository
from src.api.models.doc_tts import DocTTSJobOut, DocTTSSubmitOut
from src.api.services.audio_files import atomic_write
from src.api.services.doc_tts import read_job_meta, validate_job_id, write_job_meta
from src.api.services.document_text import DocumentExtractionError, sniff_kind
from src.api.services.signed_links import SignedLinkError, build_signed_path
from src.api.services.temporal_admin import TemporalAdminService, new_document_run_id
from src.core.config import settings

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["documents"])

#: audio_jobs.collection value grouping every document job in the GUI listing.
JOB_COLLECTION = "documents"
JOB_KIND = "doc_tts"


def _uploads_dir() -> Path:
    """Return the directory holding uploaded documents (created on demand).

    Returns:
        Path of ``<doc_tts_dir>/uploads``.
    """
    path = Path(settings.doc_tts_dir) / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _read_upload(upload: UploadFile) -> bytes:
    """Read the upload bounded by ``doc_tts_max_file_bytes``.

    Args:
        upload: The multipart file part.

    Returns:
        The raw bytes.

    Raises:
        HTTPException: 422 when the body is empty, 413 when it is too large.
    """
    limit = settings.doc_tts_max_file_bytes
    data = await upload.read(limit + 1)
    if not data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="uploaded file is empty",
        )
    if len(data) > limit:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file larger than {limit} bytes (doc_tts_max_file_bytes)",
        )
    return data


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
        HTTPException: 403 when disabled, 413 when too large, 415 for an
            unsupported type, 422 for an empty file, 502 when Temporal cannot
            start the workflow, 503 when no link secret is configured.
    """
    if not settings.doc_tts_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Document TTS is disabled (DOC_TTS_ENABLED=false)",
        )
    if not settings.doc_tts_link_secret.strip():
        # The whole point is a URL the operator can open: without a secret the
        # link cannot be signed, so fail here instead of after 10 minutes.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DOC_TTS_LINK_SECRET is not configured; signed audio links are off",
        )
    declared = request.headers.get("content-length", "")
    overhead = 1_000_000  # multipart framing and the other form fields
    if declared.isdigit() and int(declared) > settings.doc_tts_max_file_bytes + overhead:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"request larger than {settings.doc_tts_max_file_bytes} bytes",
        )

    filename = Path(file.filename or "").name.strip()
    if not filename:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="file part has no filename",
        )
    try:
        kind = sniff_kind(filename)
    except DocumentExtractionError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(exc),
        ) from exc
    data = await _read_upload(file)

    workflow_id = new_document_run_id()
    upload_path = _uploads_dir() / f"{workflow_id}{Path(filename).suffix.lower()}"
    atomic_write(upload_path, data)
    clean_title = (title or "").strip() or Path(filename).stem
    write_job_meta(
        workflow_id,
        {
            "title": clean_title,
            "filename": filename,
            "kind": kind,
            "voice": (voice or "").strip(),
            "language": (language or "").strip(),
        },
    )

    repo = AudioJobsRepository()
    await repo.start_job(workflow_id, collection=JOB_COLLECTION, kind=JOB_KIND)
    try:
        from src.workers.doc_tts_workflows import DocTTSConfig

        await TemporalAdminService().start_document_tts(
            DocTTSConfig(
                source_path=str(upload_path),
                filename=filename,
                title=(title or "").strip(),
                voice=(voice or "").strip(),
                language=(language or "").strip(),
            ),
            workflow_id,
        )
    except Exception as exc:
        logger.error("doc_tts_start_failed", workflow_id=workflow_id, error=str(exc), exc_info=True)
        await repo.finish_job(
            workflow_id,
            status="FINISHED_ERROR",
            error=f"could not start the workflow: {exc}",
            kind=JOB_KIND,
        )
        upload_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not start the document TTS workflow",
        ) from exc

    logger.info(
        "doc_tts_submitted",
        workflow_id=workflow_id,
        filename=filename,
        kind=kind,
        bytes=len(data),
    )
    return DocTTSSubmitOut(
        status="ok",
        workflow_id=workflow_id,
        title=clean_title,
        filename=filename,
        status_url=f"/api/admin/documents/tts/{workflow_id}",
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
        chunk_count=int(meta.get("chunk_count") or 0),
        audio_url=audio_url,
        error=job.get("error"),
        elapsed_seconds=job.get("elapsed_seconds"),
    )
