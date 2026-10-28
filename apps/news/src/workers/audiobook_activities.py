"""Audiobook activities."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import structlog
from temporalio import activity
from temporalio.exceptions import ApplicationError

from src.api.db.database import AudioJobsRepository
from src.api.services.audiobook import AUDIOBOOK_KIND, AudiobookService
from src.api.services.document_text import extract_document
from src.api.services.tts_lease import PRIORITY_RENDER, RenderGate, TTSLeaseService

logger = structlog.get_logger(__name__)


@activity.defn
async def extract_audiobook_activity(payload: dict[str, Any]) -> dict[str, Any]:
    run_id = str(payload.get("run_id") or "")
    filename = str(payload.get("filename") or "")
    source_path = str(payload.get("source_path") or "")
    repo = AudioJobsRepository()
    await repo.set_generating(run_id, kind=AUDIOBOOK_KIND)
    try:
        path = Path(source_path)
        data = path.read_bytes()
        doc = extract_document(data, filename)
        return {"text": doc.text, "title": doc.title, "filename": filename, "kind": doc.kind}
    except Exception as exc:
        await repo.finish_job(
            run_id, status="FINISHED_ERROR", error=str(exc), kind=AUDIOBOOK_KIND
        )
        raise ApplicationError(f"audiobook extract failed: {exc}", non_retryable=True) from exc


@activity.defn
async def render_audiobook_activity(payload: dict[str, Any]) -> dict[str, Any]:
    run_id = str(payload.get("run_id") or "")
    text = str(payload.get("text") or "")
    title = str(payload.get("title") or "")
    repo = AudioJobsRepository()
    await repo.set_generating(run_id, kind=AUDIOBOOK_KIND)
    started = time.monotonic()

    async def _tick() -> None:
        activity.heartbeat()

    async def _on_state(paused: bool) -> None:
        await repo.set_status(
            run_id, "PAUSED" if paused else "GENERATING", kind=AUDIOBOOK_KIND
        )

    lease = TTSLeaseService()
    try:
        async with lease.hold(run_id, PRIORITY_RENDER, on_tick=_tick):
            gate = RenderGate(
                lease, run_id, PRIORITY_RENDER, on_tick=_tick, on_state=_on_state
            )
            service = AudiobookService()
            res = await service.render_m4b(
                text=text,
                title=title,
                voice=str(payload.get("voice") or ""),
                language=str(payload.get("language") or "es"),
                gate=gate,
                run_id=run_id,
            )
        elapsed = round(time.monotonic() - started, 2)
        try:
            await repo.finish_job(
                run_id,
                status="FINISHED_OK",
                audio_path=res.m4b_path,
                elapsed_seconds=elapsed,
                kind=AUDIOBOOK_KIND,
            )
        except TypeError:
            await repo.finish_job(
                run_id,
                status="FINISHED_OK",
                audio_path=res.m4b_path,
                kind=AUDIOBOOK_KIND,
            )
        return {
            "audio_path": res.m4b_path,
            "chapters": res.chapters,
            "duration_seconds": res.duration_seconds,
        }
    except Exception as exc:
        try:
            await repo.finish_job(
                run_id,
                status="FINISHED_ERROR",
                error=str(exc),
                kind=AUDIOBOOK_KIND,
            )
        except Exception:
            pass
        raise ApplicationError(f"audiobook render failed: {exc}", non_retryable=True) from exc
