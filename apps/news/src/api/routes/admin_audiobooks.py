"""Admin endpoints for audiobook generation (Fase B2)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from src.api.db.database import AudioJobsRepository
from src.api.services.audiobook import (
    AUDIOBOOK_KIND,
    AudiobookService,
    digest_prefix_from_audio_path,
    parse_chapter_range,
    submit_audiobook,
)
from src.api.services.doc_tts import (
    SubmitError,
    get_job_audio_path,
    read_job_meta,
)
from src.api.services.tts_estimate import eta_seconds

router = APIRouter(prefix="/admin/audiobooks", tags=["admin-audiobooks"])

#: Statuses after which pause/resume no longer mean anything.
TERMINAL_STATUSES = frozenset({"FINISHED_OK", "FINISHED_ERROR"})


async def _job_payload(workflow_id: str) -> dict:
    """Build the status/progress document shared by the status endpoints.

    Args:
        workflow_id: Workflow id of the audiobook run.

    Returns:
        Dict with the job row, the metadata sidecar and the derived progress,
        range, pause flag and download URLs.

    Raises:
        HTTPException: 404 when neither the job row nor the metadata exists.
    """
    meta = read_job_meta(workflow_id)
    job = await AudioJobsRepository().get_job(workflow_id, kind=AUDIOBOOK_KIND)
    if job is None and not meta:
        raise HTTPException(status_code=404, detail="unknown audiobook run")
    job = job or {}
    audio_path = job.get("audio_path") or await get_job_audio_path(workflow_id)
    progress = meta.get("progress") if isinstance(meta.get("progress"), dict) else {}
    done = int((progress or {}).get("done") or 0)
    total = int((progress or {}).get("total") or 0) or int(meta.get("chapters_total") or 0)
    status = str(job.get("status") or "")
    chapter_start = job.get("chapter_start")
    chapter_end = job.get("chapter_end")
    if chapter_start is None:
        chapter_start = meta.get("chapter_start")
    if chapter_end is None:
        chapter_end = meta.get("chapter_end")
    digest = str(meta.get("digest") or digest_prefix_from_audio_path(audio_path))
    estimate = meta.get("estimate") if isinstance(meta.get("estimate"), dict) else None
    partial_ok = done > 0 or status in ("GENERATING", "PAUSED")
    return {
        "workflow_id": workflow_id,
        "title": meta.get("title"),
        "filename": meta.get("filename"),
        "kind": meta.get("kind"),
        "status": status,
        "error": job.get("error"),
        "audio_path": audio_path,
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "elapsed_seconds": job.get("elapsed_seconds"),
        "paused": bool(job.get("paused")),
        "chapter_start": chapter_start,
        "chapter_end": chapter_end,
        "chapters_done": done,
        "chapters_total": total,
        "chapters_rendered": int(meta.get("chapters_total") or 0),
        "digest": digest,
        "progress": progress,
        "eta_seconds": eta_seconds(estimate, progress),
        "download_url": (f"/api/admin/audiobooks/{workflow_id}/download" if audio_path else None),
        "partial_url": (f"/api/admin/audiobooks/{workflow_id}/partial" if partial_ok else None),
    }


async def _set_paused(workflow_id: str, paused: bool) -> dict:
    """Pause or resume a running audiobook render.

    Args:
        workflow_id: Workflow id of the run.
        paused: True to pause, False to resume.

    Returns:
        Status dict with the new pause flag.

    Raises:
        HTTPException: 404 for an unknown run, 409 when it already finished.
    """
    repo = AudioJobsRepository()
    job = await repo.get_job(workflow_id, kind=AUDIOBOOK_KIND)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown audiobook run")
    status = str(job.get("status") or "")
    if status in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="the audiobook run has already finished",
        )
    await repo.set_paused(workflow_id, paused, kind=AUDIOBOOK_KIND)
    return {
        "status": "ok",
        "workflow_id": workflow_id,
        "paused": paused,
        "job_status": status,
    }


@router.post("/submit")
async def submit_audiobook_tts(
    file: UploadFile = File(...),
    title: str | None = Form(None),
    voice: str | None = Form(None),
    language: str | None = Form(None),
    engine: str | None = Form(None),
    chapter_start: str | None = Form(None),
    chapter_end: str | None = Form(None),
    cover: UploadFile | None = File(None),
    content_length: str | None = Header(None),
):
    try:
        start, end = parse_chapter_range(chapter_start, chapter_end)
        result = await submit_audiobook(
            upload=file,
            content_length=content_length or "",
            voice=voice,
            language=language,
            title=title,
            engine=engine,
            cover=cover,
            chapter_start=start,
            chapter_end=end,
        )
    except SubmitError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return result


@router.get("/latest")
async def audiobook_latest():
    """Return the most recent audiobook run, including progress and controls."""
    jobs = await AudioJobsRepository().list_jobs(kind=AUDIOBOOK_KIND)
    if not jobs:
        raise HTTPException(status_code=404, detail="no audiobook runs yet")
    return await _job_payload(str(jobs[0].get("run_id") or ""))


@router.get("/{workflow_id}")
async def audiobook_status(workflow_id: str):
    return await _job_payload(workflow_id)


@router.get("/{workflow_id}/progress")
async def audiobook_progress(workflow_id: str):
    """Return the live progress of an audiobook run."""
    return await _job_payload(workflow_id)


@router.post("/{workflow_id}/pause")
async def audiobook_pause(workflow_id: str):
    """Ask a running render to pause after its current chunk."""
    return await _set_paused(workflow_id, True)


@router.post("/{workflow_id}/resume")
async def audiobook_resume(workflow_id: str):
    """Resume a paused render."""
    return await _set_paused(workflow_id, False)


@router.get("/{workflow_id}/partial")
async def audiobook_partial(
    workflow_id: str,
    up_to: int | None = Query(None, ge=1, description="Only the first N finished chapters"),
):
    """Mux the chapters finished so far into a preview M4B.

    Building the file runs ffmpeg, so the work happens in a worker thread; the
    result is cached by digest and chapter span.
    """
    meta = read_job_meta(workflow_id)
    job = await AudioJobsRepository().get_job(workflow_id, kind=AUDIOBOOK_KIND)
    if job is None and not meta:
        raise HTTPException(status_code=404, detail="unknown audiobook run")
    audio_path = (job or {}).get("audio_path") or await get_job_audio_path(workflow_id)
    digest = str(meta.get("digest") or digest_prefix_from_audio_path(audio_path))
    title = str(meta.get("title") or workflow_id)
    service = AudiobookService()
    try:
        path, _count = await asyncio.to_thread(
            service.build_partial_m4b,
            digest_prefix=digest,
            title=title,
            up_to=up_to,
            run_id=workflow_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        await AudioJobsRepository().set_partial_path(workflow_id, str(path), kind=AUDIOBOOK_KIND)
    except Exception:
        pass
    return FileResponse(path, media_type="audio/mp4", filename=path.name)


@router.get("/{workflow_id}/download")
async def audiobook_download(workflow_id: str):
    audio_path = await get_job_audio_path(workflow_id)
    if not audio_path:
        raise HTTPException(status_code=404, detail="audiobook not ready")
    path = Path(audio_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="file not found")
    filename = path.name
    return FileResponse(path, media_type="audio/mp4", filename=filename)
