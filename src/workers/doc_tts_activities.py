"""Temporal activities for the long-document TTS worker (Phase 7.11).

These activities are registered **only** on the doc-tts worker (see
``src/workers/run_doc_tts_worker.py``), which polls its own task queue. The
API submits the workflow; nothing else runs in the API process: extraction and
synthesis both belong to the worker so a long render cannot stall the HTTP
event loop.

Everything with side effects lives here: the workflow only orchestrates.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import structlog
from temporalio import activity
from temporalio.exceptions import ApplicationError

from src.api.db.database import AudioJobsRepository
from src.api.services.audio_files import atomic_write
from src.api.services.doc_tts import (
    DocTTSService,
    job_text_path,
    update_job_meta,
)
from src.api.services.document_text import DocumentExtractionError, extract_document
from src.core.config import get_settings

logger = structlog.get_logger(__name__)

#: ``audio_jobs.kind`` discriminator owned by this module, so document jobs
#: never collide with the digest ``'tts'`` or the ``'narration'`` rows.
DOC_TTS_JOB_KIND = "doc_tts"
#: Pseudo-collection grouping document jobs in the admin GUI listing.
DOC_TTS_JOB_COLLECTION = "documents"


@activity.defn
async def extract_document_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Read an uploaded document, extract its text and persist it on disk.

    The text file (not the document bytes) is what the synthesis activity
    consumes: keeping Temporal payloads small matters when the extracted text
    is hundreds of kilobytes.

    Args:
        payload: Dict with 'run_id' (workflow id), 'source_path' (upload),
            'filename' (original name) and optional 'title'.

    Returns:
        Dict with 'text_path', 'title', 'kind' and 'char_count'.

    Raises:
        ApplicationError: Non-retryable when the document cannot be read,
            parsed or is over the configured limits; the audio job row is
            recorded as FINISHED_ERROR first so the API can explain why.
    """
    run_id = str(payload.get("run_id") or "")
    filename = str(payload.get("filename") or "")
    source_path = str(payload.get("source_path") or "")
    if not run_id:
        raise ApplicationError("extract_document_activity: missing run_id", non_retryable=True)
    activity.logger.info(f"Extracting document {filename} for {run_id}")
    repo = AudioJobsRepository()
    await repo.set_generating(run_id, kind=DOC_TTS_JOB_KIND)

    try:
        path = Path(source_path)
        if not source_path or not path.is_file():
            raise DocumentExtractionError("uploaded file is missing on disk")
        settings = get_settings()
        data = path.read_bytes()
        if len(data) > settings.doc_tts_max_file_bytes:
            raise DocumentExtractionError(
                f"file too large: {len(data)} bytes "
                f"(doc_tts_max_file_bytes={settings.doc_tts_max_file_bytes})"
            )
        document = extract_document(data, filename)
    except DocumentExtractionError as exc:
        await repo.finish_job(
            run_id, status="FINISHED_ERROR", error=str(exc), kind=DOC_TTS_JOB_KIND
        )
        raise ApplicationError(f"Document extraction failed: {exc}", non_retryable=True) from exc
    except OSError as exc:
        await repo.finish_job(
            run_id, status="FINISHED_ERROR", error=str(exc), kind=DOC_TTS_JOB_KIND
        )
        raise ApplicationError(f"Cannot read the uploaded file: {exc}", non_retryable=True) from exc

    title = str(payload.get("title") or "").strip() or document.title
    try:
        text_path = job_text_path(run_id)
        atomic_write(text_path, document.text.encode("utf-8"))
    except (OSError, ValueError) as exc:
        await repo.finish_job(
            run_id, status="FINISHED_ERROR", error=str(exc), kind=DOC_TTS_JOB_KIND
        )
        raise ApplicationError(
            f"Cannot store the extracted text: {exc}", non_retryable=True
        ) from exc

    update_job_meta(
        run_id,
        title=title,
        filename=filename,
        kind=document.kind,
        char_count=document.char_count,
    )
    activity.logger.info(f"Document extracted: kind={document.kind} chars={document.char_count}")
    return {
        "text_path": str(text_path),
        "title": title,
        "kind": document.kind,
        "char_count": document.char_count,
    }


@activity.defn
async def synthesize_document_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Synthesize the extracted text into one MP3 and close the job row.

    The service heartbeats after every finished chunk, so a stalled Edge call
    trips ``heartbeat_timeout`` instead of silently occupying the queue for
    hours; already-synthesized chunks stay cached and a retry resumes there.

    Args:
        payload: Dict with 'run_id', 'text_path', 'title', 'voice' and
            'language'.

    Returns:
        Dict with audio_path, voice, char_count, chunk_count, duration_estimate
        and cached.

    Raises:
        ApplicationError: Non-retryable when synthesis fails; the audio job
            row is recorded as FINISHED_ERROR first.
    """
    run_id = str(payload.get("run_id") or "")
    text_path = str(payload.get("text_path") or "")
    if not run_id:
        raise ApplicationError("synthesize_document_activity: missing run_id", non_retryable=True)
    title = str(payload.get("title") or "")
    voice = str(payload.get("voice") or "")
    language = str(payload.get("language") or "")
    activity.logger.info(f"Synthesizing document audio for {run_id} ({title})")

    repo = AudioJobsRepository()
    await repo.set_generating(run_id, kind=DOC_TTS_JOB_KIND)
    started = time.monotonic()

    def _progress(done: int, total: int) -> None:
        activity.heartbeat(done, total)

    try:
        text = Path(text_path).read_text(encoding="utf-8")
        result = await DocTTSService().synthesize_document(
            text=text,
            voice=voice,
            language=language,
            title=title,
            on_progress=_progress,
        )
    except Exception as exc:
        await repo.finish_job(
            run_id,
            status="FINISHED_ERROR",
            error=str(exc),
            elapsed_seconds=round(time.monotonic() - started, 2),
            kind=DOC_TTS_JOB_KIND,
        )
        raise ApplicationError(f"Document TTS failed: {exc}", non_retryable=True) from exc

    elapsed = round(time.monotonic() - started, 2)
    await repo.finish_job(
        run_id,
        status="FINISHED_OK",
        audio_path=result.audio_path,
        elapsed_seconds=elapsed,
        kind=DOC_TTS_JOB_KIND,
    )
    update_job_meta(
        run_id,
        title=result.title or title,
        voice=result.voice,
        language=result.language,
        char_count=result.char_count,
        chunk_count=result.chunk_count,
        duration_estimate=result.duration_estimate,
        cached=result.cached,
    )
    activity.logger.info(
        f"Document audio ready: {result.audio_path} ({result.chunk_count} chunks, {elapsed}s)"
    )
    return {
        "audio_path": result.audio_path,
        "voice": result.voice,
        "language": result.language,
        "title": result.title or title,
        "char_count": result.char_count,
        "chunk_count": result.chunk_count,
        "duration_estimate": result.duration_estimate,
        "cached": result.cached,
    }
