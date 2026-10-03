"""Temporal workflow for the long-document TTS pipeline (Phase 7.11).

The document spoke has its own workflow **and** its own task queue: a 300-page
render holds an activity slot for a long time, and it must never delay a daily
digest. The workflow only orchestrates; the upload, the extraction and the
synthesis all live in activities (see ``src/workers/doc_tts_activities.py``).

Both activities are addressed directly (no ``task_queue`` override): they run
on the queue this workflow was scheduled on, which is served by the doc-tts
worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from src.workers.doc_tts_activities import (
        extract_document_activity,
        synthesize_document_activity,
    )

# Reading a file and parsing it (PDF/EPUB) is local work; the ceiling only has
# to absorb a pathological document.
DOC_TTS_EXTRACT_TIMEOUT = timedelta(minutes=15)
# 300 pages ≈ 600k chars ≈ 150 chunks, each ~20 s, 4 in parallel ≈ 12 min, plus
# Edge retries. Bound the whole synthesis far above the expected runtime while
# heartbeats keep a stalled run detectable.
DOC_TTS_SYNTHESIZE_TIMEOUT = timedelta(hours=6)
DOC_TTS_HEARTBEAT_TIMEOUT = timedelta(seconds=600)
# Extraction failures are raised non-retryable, so this policy only ever
# applies to genuinely transient errors (a locked disk, a flaky read).
DOC_TTS_RETRY_POLICY = RetryPolicy(maximum_attempts=3)


@dataclass
class DocTTSConfig:
    """Input configuration for one document TTS run (Phase 7.11).

    The workflow id (``doc-tts-<hex>``) is assigned by the API that accepted
    the upload, because the upload, the metadata and the job row are keyed by
    it; activities read it back from ``workflow.info().workflow_id`` instead of
    trusting a copy in this payload.
    """

    source_path: str = ""
    """Absolute path of the uploaded document (shared ./data volume)."""
    filename: str = ""
    """Original filename; drives the kind sniffing and the fallback title."""
    title: str = ""
    """Operator-provided title; the extracted metadata wins when empty."""
    voice: str = ""
    """Requested Edge voice; empty uses the configured default."""
    language: str = ""
    """ISO 639-1 language; empty uses the configured default."""


@workflow.defn
class DocumentTtsWorkflow:
    """Extract an uploaded document and synthesize it into a single MP3."""

    @workflow.run
    async def run(self, config: DocTTSConfig) -> dict[str, Any]:
        """Run extract -> synthesize for one document.

        Args:
            config: Paths and voice options resolved by the submitting API.

        Returns:
            Dict with status, run_id, audio_path, voice, char_count and the
            chunk/duration metadata of the finished audio.

        Raises:
            ApplicationError: When extraction or synthesis fails permanently.
                The activity has already recorded the job as FINISHED_ERROR,
                so the status endpoint reports the reason either way.
        """
        run_id = workflow.info().workflow_id
        workflow.logger.info(f"Document TTS started: {config.filename} ({run_id})")

        # Re-raising with the step and the underlying reason keeps the Temporal
        # UI from showing a bare "Activity task failed"; the audio job row
        # already carries the same detail for the status endpoint.
        step = "extract"
        try:
            extracted = await workflow.execute_activity(
                extract_document_activity,
                {
                    "run_id": run_id,
                    "source_path": config.source_path,
                    "filename": config.filename,
                    "title": config.title,
                },
                start_to_close_timeout=DOC_TTS_EXTRACT_TIMEOUT,
                retry_policy=DOC_TTS_RETRY_POLICY,
            )

            step = "synthesize"
            result = await workflow.execute_activity(
                synthesize_document_activity,
                {
                    "run_id": run_id,
                    "text_path": extracted["text_path"],
                    "title": extracted["title"],
                    "voice": config.voice,
                    "language": config.language,
                },
                start_to_close_timeout=DOC_TTS_SYNTHESIZE_TIMEOUT,
                heartbeat_timeout=DOC_TTS_HEARTBEAT_TIMEOUT,
                retry_policy=DOC_TTS_RETRY_POLICY,
            )
        except (ActivityError, ApplicationError) as exc:
            detail = str(getattr(exc, "cause", None) or exc)
            raise ApplicationError(
                f"Document TTS {step} failed: {detail}", non_retryable=True
            ) from exc

        workflow.logger.info(f"Document TTS completed: {run_id} -> {result.get('audio_path')}")
        return {"status": "ok", "run_id": run_id, **result}
