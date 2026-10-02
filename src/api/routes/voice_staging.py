"""Staging endpoint for reference audio designed off-box (Phase 7.9).

The design worker on the GPU box POSTs the gzipped WAV here instead of
returning it through Temporal history, where a 24-voice batch was big enough
for the server to terminate the workflow with ``Workflow history size exceeds
limit``. The store activity later reads the same file from ``data/voices/
.staging``.

Protected by its own shared key (``VOICE_STAGING_API_KEY``), not by the admin
middleware: the caller is a worker on another machine, not a browser session.
Fails closed while the key is unset.
"""

from __future__ import annotations

import secrets

import structlog
from fastapi import APIRouter, HTTPException, Request

from src.api.services.voice_staging import (
    MAX_STAGED_BYTES,
    STAGING_KEY_HEADER,
    VoiceStagingError,
    write_staged,
)
from src.core.config import settings

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["voice-staging"])

#: Slack over MAX_STAGED_BYTES for the framing the caller adds around the
#: gzip stream.
MAX_REQUEST_BYTES = MAX_STAGED_BYTES + 64 * 1024


@router.post("/internal/voices/staged/{name}")
async def stage_reference(name: str, request: Request) -> dict[str, object]:
    """Store a gzipped reference WAV for the store activity to pick up.

    Args:
        name: Profile name (snake_case), which becomes the file name.
        request: Raw gzip body plus the ``X-Voice-Staging-Key`` header.

    Returns:
        Acknowledgement with the number of bytes written.

    Raises:
        HTTPException: 503 while staging is unconfigured, 401 on a bad key,
            422 on a payload the store activity could not read back.
    """
    expected = settings.voice_staging_api_key
    provided = request.headers.get(STAGING_KEY_HEADER, "")
    if not expected:
        raise HTTPException(status_code=503, detail="voice staging is not configured")
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid staging key")

    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > MAX_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")

    body = await request.body()
    try:
        written = write_staged(name, body)
    except VoiceStagingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    logger.info("voice_reference_staged", name=name, bytes=written)
    return {"status": "ok", "name": name, "bytes": written}
