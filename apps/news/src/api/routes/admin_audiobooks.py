"""Admin endpoints for audiobook generation (Fase B2)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

from src.api.services.audiobook import submit_audiobook
from src.api.services.doc_tts import (
    SubmitError,
    get_job_audio_path,
    read_job_meta,
)

router = APIRouter(prefix="/api/admin/audiobooks", tags=["admin-audiobooks"])


@router.post("/submit")
async def submit_audiobook_tts(
    file: UploadFile = File(...),
    title: str | None = Form(None),
    voice: str | None = Form(None),
    language: str | None = Form(None),
    engine: str | None = Form(None),
    cover: UploadFile | None = File(None),
    content_length: str | None = Header(None),
):
    try:
        result = await submit_audiobook(
            upload=file,
            content_length=content_length or "",
            voice=voice,
            language=language,
            title=title,
            engine=engine,
            cover=cover,
        )
    except SubmitError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return result


@router.get("/{workflow_id}")
async def audiobook_status(workflow_id: str):
    meta = read_job_meta(workflow_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="unknown audiobook run")
    audio_path = get_job_audio_path(workflow_id)
    return {
        "workflow_id": workflow_id,
        "title": meta.get("title"),
        "filename": meta.get("filename"),
        "kind": meta.get("kind"),
        "audio_path": audio_path,
        "download_url": f"/api/admin/audiobooks/{workflow_id}/download" if audio_path else None,
    }


@router.get("/{workflow_id}/download")
async def audiobook_download(workflow_id: str):
    audio_path = get_job_audio_path(workflow_id)
    if not audio_path:
        raise HTTPException(status_code=404, detail="audiobook not ready")
    path = Path(audio_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="file not found")
    filename = path.name
    return FileResponse(path, media_type="audio/mp4", filename=filename)
