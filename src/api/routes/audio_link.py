"""Signed audio link route (Phase 7.11): serve document MP3s by link.

The only endpoint of this project that is intentionally **not** behind the
admin middleware: its authorization is the HMAC signature in the query string
(``exp`` + ``sig``), which is what makes the URL usable from a phone browser
over Tailscale without carrying the admin token. Links expire
(``doc_tts_link_ttl_seconds``) and are unforgeable without ``doc_tts_link_secret``.
"""

from __future__ import annotations

from pathlib import Path

import structlog
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse

from src.api.db.database import AudioJobsRepository
from src.api.services.signed_links import SignedLinkError, verify_signed_link
from src.core.config import settings

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["documents"])

JOB_KIND = "doc_tts"


@router.get(
    "/audio/doc/{workflow_id}",
    summary="Download a document audio through a signed link",
    description=(
        "Serves the finished MP3 of a document job. No admin token: the "
        "``exp``/``sig`` query parameters are the authorization. Expired or "
        "forged links answer 403."
    ),
    response_class=FileResponse,
)
async def get_document_audio(
    workflow_id: str,
    exp: int = Query(..., description="Expiry unix timestamp of the link"),
    sig: str = Query(..., description="HMAC-SHA256 signature of '<workflow_id>:<exp>'"),
) -> FileResponse:
    """GET /audio/doc/{workflow_id} endpoint handler.

    Args:
        workflow_id: Workflow id of the document job.
        exp: Expiry timestamp from the signed link.
        sig: Signature from the signed link.

    Returns:
        FileResponse streaming the MP3.

    Raises:
        HTTPException: 403 for an invalid/expired/forged link, 404 when the
            job does not exist, has not finished or its file is gone.
    """
    try:
        verify_signed_link(workflow_id, exp, sig)
    except SignedLinkError as exc:
        logger.info("audio_link_rejected", workflow_id=workflow_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid or expired link",
        ) from exc

    job = await AudioJobsRepository().get_job(workflow_id, kind=JOB_KIND)
    if job is None or job["status"] != "FINISHED_OK" or not job.get("audio_path"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="audio not available")

    # The path comes from our own DB row, but resolve it and confine it to the
    # audio directory anyway: no row should ever be able to serve a foreign file.
    audio_path = Path(str(job["audio_path"])).resolve()
    audio_dir = (Path(settings.doc_tts_dir) / "audio").resolve()
    if not audio_path.is_relative_to(audio_dir) or not audio_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="audio not available")
    return FileResponse(audio_path, media_type="audio/mpeg", filename=audio_path.name)
