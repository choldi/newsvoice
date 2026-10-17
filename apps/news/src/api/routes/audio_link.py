"""Signed audio link routes (Phase 7.11/7.12): serve audio by link.

The only endpoints of this project that are intentionally **not** behind the
admin middleware: their authorization is the HMAC signature in the query string
(``exp`` + ``sig``), which is what makes a URL usable from a phone browser over
Tailscale without carrying the admin token. Links expire
(``doc_tts_link_ttl_seconds``) and are unforgeable without
``doc_tts_link_secret``.

Three kinds share one implementation, each confined to its own directory:

- ``/audio/doc/{id}``       → ``doc_tts`` jobs, ``<doc_tts_dir>/audio``
- ``/audio/lesson/{id}``    → ``lesson`` jobs, the narration cache
- ``/audio/narration/{id}`` → ``narration`` jobs, the narration cache
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

import structlog
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse

from src.api.db.database import AudioJobsRepository
from src.api.services.signed_links import SignedLinkError, verify_signed_link
from src.core.config import settings

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["documents"])


def _confined_file(path_str: str, root: Path) -> Path | None:
    """Resolve a stored audio path and confine it to its directory.

    The path comes from our own DB row, but no row should ever be able to
    serve a foreign file: resolve it and require it to live under ``root``.

    Args:
        path_str: Stored ``audio_path`` value.
        root: Only files under this directory may be served.

    Returns:
        The resolved path when it is inside ``root`` and exists, else None.
    """
    resolved = Path(path_str).resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        return None
    return resolved


async def _serve_signed_audio(
    workflow_id: str, exp: int, sig: str, *, kind: str, root: Path
) -> FileResponse:
    """Verify a signed link and stream the finished audio it points at.

    Args:
        workflow_id: Workflow id (job key) from the path.
        exp: Expiry timestamp from the query string.
        sig: Signature from the query string.
        kind: ``audio_jobs.kind`` to look up.
        root: Directory the file must live in.

    Returns:
        FileResponse streaming the audio.

    Raises:
        HTTPException: 403 for an invalid/expired/forged link, 404 when the
            job does not exist, has not finished or its file is gone.
    """
    try:
        verify_signed_link(workflow_id, exp, sig)
    except SignedLinkError as exc:
        logger.info("audio_link_rejected", workflow_id=workflow_id, kind=kind, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid or expired link",
        ) from exc

    job = await AudioJobsRepository().get_job(workflow_id, kind=kind)
    if job is None or job["status"] != "FINISHED_OK" or not job.get("audio_path"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="audio not available")

    audio_path = _confined_file(str(job["audio_path"]), root)
    if audio_path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="audio not available")
    media_type, _ = mimetypes.guess_type(str(audio_path))
    return FileResponse(audio_path, media_type=media_type or "audio/mpeg", filename=audio_path.name)


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
    return await _serve_signed_audio(
        workflow_id,
        exp,
        sig,
        kind="doc_tts",
        root=Path(settings.doc_tts_dir) / "audio",
    )


@router.get(
    "/audio/lesson/{workflow_id}",
    summary="Download a lesson audio through a signed link",
    description=(
        "Serves the rendered audio of a lesson. No admin token: the "
        "``exp``/``sig`` query parameters are the authorization (Phase 7.12, "
        "same secret as the document links). Expired or forged links answer 403."
    ),
    response_class=FileResponse,
)
async def get_lesson_audio(
    workflow_id: str,
    exp: int = Query(..., description="Expiry unix timestamp of the link"),
    sig: str = Query(..., description="HMAC-SHA256 signature of '<workflow_id>:<exp>'"),
) -> FileResponse:
    """GET /audio/lesson/{workflow_id} endpoint handler.

    Args:
        workflow_id: Workflow id of the lesson job.
        exp: Expiry timestamp from the signed link.
        sig: Signature from the signed link.

    Returns:
        FileResponse streaming the lesson audio (M4A).

    Raises:
        HTTPException: 403 for an invalid/expired/forged link, 404 when the
            job does not exist, has not finished or its file is gone.
    """
    return await _serve_signed_audio(
        workflow_id,
        exp,
        sig,
        kind="lesson",
        root=Path(settings.narration_cache_dir),
    )


@router.get(
    "/audio/narration/{workflow_id}",
    summary="Download a narration audio through a signed link",
    description=(
        "Serves the rendered audio of a podcast/narration job (Phase 7.12 GUI "
        "tools page). No admin token: the ``exp``/``sig`` query parameters are "
        "the authorization. Expired or forged links answer 403."
    ),
    response_class=FileResponse,
)
async def get_narration_audio(
    workflow_id: str,
    exp: int = Query(..., description="Expiry unix timestamp of the link"),
    sig: str = Query(..., description="HMAC-SHA256 signature of '<workflow_id>:<exp>'"),
) -> FileResponse:
    """GET /audio/narration/{workflow_id} endpoint handler.

    Args:
        workflow_id: Job key of the narration (the digest run id when attached
            to a digest, the narration workflow id for standalone renders).
        exp: Expiry timestamp from the signed link.
        sig: Signature from the signed link.

    Returns:
        FileResponse streaming the narration audio (M4A).

    Raises:
        HTTPException: 403 for an invalid/expired/forged link, 404 when the
            job does not exist, has not finished or its file is gone.
    """
    return await _serve_signed_audio(
        workflow_id,
        exp,
        sig,
        kind="narration",
        root=Path(settings.narration_cache_dir),
    )
