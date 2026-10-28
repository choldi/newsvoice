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
        render_audiobook_activity,
    )


@dataclass
class AudiobookConfig:
    source_path: str = ""
    filename: str = ""
    title: str = ""
    voice: str = ""
    language: str = "es"


@workflow.defn
class AudiobookWorkflow:
    @workflow.run
    async def run(self, config: AudiobookConfig) -> dict[str, Any]:
        run_id = workflow.info().workflow_id
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
        result = await workflow.execute_activity(
            render_audiobook_activity,
            {
                "run_id": run_id,
                "text": extracted["text"],
                "title": extracted.get("title") or config.title,
                "voice": config.voice,
                "language": config.language,
            },
            start_to_close_timeout=timedelta(hours=6),
            heartbeat_timeout=timedelta(seconds=600),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        return {"status": "ok", "run_id": run_id, **result}
