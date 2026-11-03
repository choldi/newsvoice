"""Integration tests for the AudiobookWorkflow failure bookkeeping."""

from __future__ import annotations

from typing import Any

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.workers.audiobook_workflows import AudiobookConfig, AudiobookWorkflow

EXTRACTED = {
    "text": "Hola mundo.",
    "title": "Libro",
    "filename": "libro.epub",
    "kind": "epub",
}
RENDERED = {
    "audio_path": "/data/audiobooks/audio/libro.m4b",
    "chapters": 2,
    "duration_seconds": 12.0,
}

EXTRACT_CALLS: list[dict[str, Any]] = []
RENDER_CALLS: list[dict[str, Any]] = []
FAIL_CALLS: list[dict[str, Any]] = []


@activity.defn(name="extract_audiobook_activity")
async def stub_extract(payload: dict[str, Any]) -> dict[str, Any]:
    EXTRACT_CALLS.append(payload)
    if payload.get("filename", "").startswith("broken"):
        raise ApplicationError("audiobook extract failed: unreadable EPUB", non_retryable=True)
    return EXTRACTED


@activity.defn(name="render_audiobook_activity")
async def stub_render(payload: dict[str, Any]) -> dict[str, Any]:
    RENDER_CALLS.append(payload)
    if payload.get("engine") == "broken":
        raise ApplicationError("audiobook render failed: engine down", non_retryable=True)
    return RENDERED


@activity.defn(name="fail_audiobook_activity")
async def stub_fail(payload: dict[str, Any]) -> None:
    FAIL_CALLS.append(payload)


def _reset() -> None:
    """Clear the per-test capture lists."""
    EXTRACT_CALLS.clear()
    RENDER_CALLS.clear()
    FAIL_CALLS.clear()


async def _run(
    config: AudiobookConfig, *, workflow_id: str = "audiobook-test", queue: str = "tq-audiobook"
) -> dict[str, Any]:
    """Run AudiobookWorkflow once against the time-skipping server."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AudiobookWorkflow],
            activities=[stub_extract, stub_render, stub_fail],
        ):
            return await env.client.execute_workflow(
                AudiobookWorkflow.run,
                config,
                id=workflow_id,
                task_queue=queue,
            )


async def _run_expecting_failure(config: AudiobookConfig, *, workflow_id: str, queue: str) -> None:
    """Run the workflow and assert that it fails."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AudiobookWorkflow],
            activities=[stub_extract, stub_render, stub_fail],
        ):
            with pytest.raises(WorkflowFailureError):
                await env.client.execute_workflow(
                    AudiobookWorkflow.run,
                    config,
                    id=workflow_id,
                    task_queue=queue,
                )


async def test_audiobook_workflow_happy_path_skips_the_safety_net() -> None:
    """A successful render returns its metadata and never records a failure."""
    _reset()
    config = AudiobookConfig(source_path="/data/uploads/libro.epub", filename="libro.epub")

    result = await _run(config)

    assert result["status"] == "ok"
    assert result["run_id"] == "audiobook-test"
    assert result["audio_path"] == RENDERED["audio_path"]
    assert RENDER_CALLS[0]["text"] == EXTRACTED["text"]
    assert FAIL_CALLS == []


async def test_render_failure_triggers_the_workflow_safety_net() -> None:
    """When the render cannot report, the workflow still closes the job."""
    _reset()
    config = AudiobookConfig(
        source_path="/data/uploads/libro.epub", filename="libro.epub", engine="broken"
    )

    await _run_expecting_failure(
        config, workflow_id="audiobook-render-fail", queue="tq-audiobook-fail"
    )

    assert len(FAIL_CALLS) == 1
    assert FAIL_CALLS[0]["run_id"] == "audiobook-render-fail"
    assert "audiobook render failed" in FAIL_CALLS[0]["error"]
    assert "engine down" in FAIL_CALLS[0]["error"]


async def test_extract_failure_triggers_the_workflow_safety_net() -> None:
    """Extraction failures are reported with the step that failed."""
    _reset()
    config = AudiobookConfig(source_path="/data/uploads/broken.epub", filename="broken.epub")

    await _run_expecting_failure(
        config, workflow_id="audiobook-extract-fail", queue="tq-audiobook-extract-fail"
    )

    assert RENDER_CALLS == [], "no render after a failed extraction"
    assert len(FAIL_CALLS) == 1
    assert "audiobook extract failed" in FAIL_CALLS[0]["error"]


async def test_chapter_range_is_forwarded_to_the_render() -> None:
    """A slice request reaches the render activity unchanged."""
    _reset()
    config = AudiobookConfig(
        source_path="/data/uploads/libro.epub",
        filename="libro.epub",
        chapter_start=2,
        chapter_end=5,
    )

    await _run(config, workflow_id="audiobook-range")

    assert RENDER_CALLS[0]["chapter_start"] == 2
    assert RENDER_CALLS[0]["chapter_end"] == 5
