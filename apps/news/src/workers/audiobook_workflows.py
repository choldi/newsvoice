"""Audiobook workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from src.workers.audiobook_activities import (
        extract_audiobook_activity,
        fail_audiobook_activity,
        render_audiobook_activity,
    )

#: Bookkeeping must never be what keeps a failed workflow open.
AUDIOBOOK_FAILURE_BOOKKEEPING_TIMEOUT = timedelta(minutes=2)


@dataclass
class AudiobookConfig:
    source_path: str = ""
    filename: str = ""
    title: str = ""
    voice: str = ""
    language: str = "es"
    engine: str = "edge"
    #: Optional 1-based inclusive chapter range (None = the whole book).
    chapter_start: int | None = None
    chapter_end: int | None = None


@workflow.defn
class AudiobookWorkflow:
    @workflow.run
    async def run(self, config: AudiobookConfig) -> dict[str, Any]:
        run_id = workflow.info().workflow_id
        step = "extract"
        try:
            extracted = await workflow.execute_activity(
                extract_audiobook_activity,
                {
                    "run_id": run_id,
                    "source_path": config.source_path,
                    "filename": config.filename,
                    "title": config.title,
                },
                start_to_close_timeout=timedelta(minutes=15),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            step = "render"
            result = await workflow.execute_activity(
                render_audiobook_activity,
                {
                    "run_id": run_id,
                    "text": extracted.get("text") or "",
                    "blocks": extracted.get("blocks") or [],
                    "title": extracted.get("title") or config.title,
                    "voice": config.voice,
                    "language": config.language,
                    "engine": config.engine,
                    "chapter_start": config.chapter_start,
                    "chapter_end": config.chapter_end,
                },
                # Paused time counts against start_to_close, and an operator
                # may pause a long book for hours; 12h leaves room for a 7h
                # render plus a long pause without changing the safety net.
                start_to_close_timeout=timedelta(hours=12),
                heartbeat_timeout=timedelta(seconds=600),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except Exception as exc:
            # Safety net: the activity records its own failures, but a run can
            # still die where no activity code gets to run (a worker crash
            # between attempts, an activity the SDK never delivered). Closing
            # the job here is what keeps the GUI from showing "generando"
            # forever. The activity is idempotent and skips terminal rows.
            detail = str(getattr(exc, "cause", None) or exc)
            try:
                await workflow.execute_activity(
                    fail_audiobook_activity,
                    {"run_id": run_id, "error": f"audiobook {step} failed: {detail}"},
                    start_to_close_timeout=AUDIOBOOK_FAILURE_BOOKKEEPING_TIMEOUT,
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            except Exception as bookkeeping_exc:
                workflow.logger.warning(
                    f"Audiobook failure bookkeeping failed for {run_id}: {bookkeeping_exc}"
                )
            raise
        return {"status": "ok", "run_id": run_id, **result}
