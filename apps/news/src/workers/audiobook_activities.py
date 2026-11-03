"""Audiobook activities."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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

#: Seconds between heartbeats while a render is synthesizing.
#:
#: The activity used to heartbeat only while waiting for/yielding the engine
#: lease, so a long book spent its whole synthesis without a single heartbeat
#: and Temporal killed it at ``heartbeat_timeout`` (600 s) — twice, exhausting
#: the retry budget. The loop below covers every phase, synthesis included.
AUDIOBOOK_HEARTBEAT_INTERVAL_SECONDS = 30.0

#: Statuses that already tell the operator how the run ended; a failure
#: bookkeeping step must never overwrite one of them.
TERMINAL_STATUSES = frozenset({"FINISHED_OK", "FINISHED_ERROR"})


async def _heartbeat_forever(stop: asyncio.Event, interval: float) -> None:
    """Heartbeat until ``stop`` is set, ignoring the ones that cannot be sent.

    Args:
        stop: Event set by the owning activity when it finishes.
        interval: Seconds between heartbeats.
    """
    while True:
        try:
            activity.heartbeat()
        except Exception as exc:  # noqa: BLE001 - a lost heartbeat must not kill the render
            logger.debug("audiobook_heartbeat_failed", error=str(exc))
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            continue


@asynccontextmanager
async def _heartbeating(interval: float) -> AsyncIterator[None]:
    """Keep the activity heartbeat alive for the whole block.

    Args:
        interval: Seconds between heartbeats.
    """
    stop = asyncio.Event()
    task = asyncio.create_task(_heartbeat_forever(stop, interval))
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _finish_error(repo: AudioJobsRepository, run_id: str, error: str) -> None:
    """Record a failed run, swallowing bookkeeping errors.

    The caller is already on its way out (usually re-raising), so a failed
    write must not mask the real reason.

    Args:
        repo: Audio job repository.
        run_id: Workflow id of the run.
        error: Human-readable failure reason.
    """
    try:
        await repo.finish_job(
            run_id,
            status="FINISHED_ERROR",
            error=error,
            kind=AUDIOBOOK_KIND,
        )
    except Exception as exc:  # noqa: BLE001 - the original error is the one worth keeping
        logger.warning("audiobook_finish_error_failed", run_id=run_id, error=str(exc))


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
    except asyncio.CancelledError:
        await _finish_error(repo, run_id, "audiobook extract cancelled")
        raise
    except Exception as exc:
        await _finish_error(repo, run_id, str(exc))
        raise ApplicationError(f"audiobook extract failed: {exc}", non_retryable=True) from exc


@activity.defn
async def render_audiobook_activity(payload: dict[str, Any]) -> dict[str, Any]:
    run_id = str(payload.get("run_id") or "")
    text = str(payload.get("text") or "")
    title = str(payload.get("title") or "")
    repo = AudioJobsRepository()
    await repo.set_generating(run_id, kind=AUDIOBOOK_KIND)
    started = time.monotonic()

    from src.api.services.tts_backends import TTSBackendError, resolve_backend

    try:
        backend = resolve_backend(str(payload.get("engine") or "edge"))
    except TTSBackendError as exc:
        await _finish_error(repo, run_id, str(exc))
        raise ApplicationError(f"audiobook engine: {exc}", non_retryable=True) from exc

    def _tick() -> None:
        activity.heartbeat()

    async def _on_state(paused: bool) -> None:
        await repo.set_status(run_id, "PAUSED" if paused else "GENERATING", kind=AUDIOBOOK_KIND)

    async def _operator_paused() -> bool:
        """Read the API-written pause flag between chunks."""
        return await repo.is_paused(run_id, kind=AUDIOBOOK_KIND)

    async def _on_progress(progress_payload: dict[str, Any]) -> None:
        """Persist the render progress in the job metadata sidecar."""
        from src.api.services.doc_tts import update_job_meta

        fields: dict[str, Any] = {}
        for key in ("digest", "chapters_total", "chapter_start", "chapter_end", "cached"):
            if key in progress_payload:
                fields[key] = progress_payload[key]
        progress = progress_payload.get("progress")
        if isinstance(progress, dict):
            fields["progress"] = progress
        if fields:
            update_job_meta(run_id, **fields)

    chapter_start = payload.get("chapter_start")
    chapter_end = payload.get("chapter_end")
    chapter_range = (
        (int(chapter_start or 1), int(chapter_end or 0))
        if chapter_start is not None or chapter_end is not None
        else None
    )

    lease = TTSLeaseService()
    try:
        async with _heartbeating(AUDIOBOOK_HEARTBEAT_INTERVAL_SECONDS):
            async with lease.hold(run_id, PRIORITY_RENDER, on_tick=_tick):
                gate = RenderGate(
                    lease,
                    run_id,
                    PRIORITY_RENDER,
                    on_tick=_tick,
                    on_state=_on_state,
                    user_paused=_operator_paused,
                )
                service = AudiobookService()
                res = await service.render_m4b(
                    text=text,
                    title=title,
                    voice=str(payload.get("voice") or ""),
                    language=str(payload.get("language") or "es"),
                    backend=backend,
                    gate=gate,
                    run_id=run_id,
                    chapter_range=chapter_range,
                    on_progress=_on_progress,
                )
        await repo.set_chapter_range(
            run_id,
            res.chapter_start,
            res.chapter_end,
            kind=AUDIOBOOK_KIND,
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
    except asyncio.CancelledError:
        # Heartbeat timeouts and worker shutdowns cancel the activity with a
        # BaseException, which ``except Exception`` never saw: the job row was
        # left GENERATING forever while the workflow was already FAILED.
        await _finish_error(
            repo,
            run_id,
            "audiobook render cancelled (heartbeat timeout or worker shutdown)",
        )
        raise
    except Exception as exc:
        await _finish_error(repo, run_id, str(exc))
        raise ApplicationError(f"audiobook render failed: {exc}", non_retryable=True) from exc


@activity.defn
async def fail_audiobook_activity(payload: dict[str, Any]) -> None:
    """Close a run the workflow gave up on, unless the activity already did.

    This is the workflow-level safety net: it runs when ``AudiobookWorkflow``
    exhausts its activity retries, which covers failures the activity itself
    could not report (for example a hard worker crash between attempts). It is
    idempotent and never overwrites a terminal status.

    Args:
        payload: Dict with ``run_id`` and ``error``.
    """
    run_id = str(payload.get("run_id") or "")
    if not run_id:
        return
    error = str(payload.get("error") or "audiobook workflow failed")
    repo = AudioJobsRepository()
    job = await repo.get_job(run_id, kind=AUDIOBOOK_KIND)
    if job is not None and str(job.get("status")) in TERMINAL_STATUSES:
        logger.info(
            "audiobook_failure_already_recorded",
            run_id=run_id,
            status=job.get("status"),
        )
        return
    await _finish_error(repo, run_id, error)
    logger.warning("audiobook_failed_by_workflow", run_id=run_id, error=error)
