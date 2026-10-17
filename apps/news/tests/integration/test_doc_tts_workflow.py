"""Integration tests for the Phase 7.11 DocumentTtsWorkflow."""

from __future__ import annotations

from typing import Any

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.workers.doc_tts_workflows import DocTTSConfig, DocumentTtsWorkflow

EXTRACTED = {
    "text_path": "/data/doc_tts/text/doc-tts-test.txt",
    "title": "Clase de espanol",
    "kind": "epub",
    "char_count": 4321,
}
SYNTHESIZED = {
    "audio_path": "/data/doc_tts/audio/doc_clase_abc123.mp3",
    "voice": "es-ES-ElviraNeural",
    "language": "es",
    "title": "Clase de espanol",
    "char_count": 4321,
    "chunk_count": 3,
    "duration_estimate": 288.07,
    "cached": False,
}

EXTRACT_CALLS: list[dict[str, Any]] = []
SYNTH_CALLS: list[dict[str, Any]] = []


@activity.defn(name="extract_document_activity")
async def stub_extract(payload: dict[str, Any]) -> dict[str, Any]:
    EXTRACT_CALLS.append(payload)
    if payload.get("filename", "").startswith("broken"):
        from temporalio.exceptions import ApplicationError

        raise ApplicationError("Document extraction failed: unreadable PDF", non_retryable=True)
    return EXTRACTED


@activity.defn(name="synthesize_document_activity")
async def stub_synthesize(payload: dict[str, Any]) -> dict[str, Any]:
    SYNTH_CALLS.append(payload)
    return SYNTHESIZED


def _reset() -> None:
    """Clear the per-test capture lists."""
    EXTRACT_CALLS.clear()
    SYNTH_CALLS.clear()


async def _run(config: DocTTSConfig, *, workflow_id: str = "doc-tts-test") -> dict[str, Any]:
    """Run DocumentTtsWorkflow once against the time-skipping server."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq-doc",
            workflows=[DocumentTtsWorkflow],
            activities=[stub_extract, stub_synthesize],
        ):
            return await env.client.execute_workflow(
                DocumentTtsWorkflow.run,
                config,
                id=workflow_id,
                task_queue="tq-doc",
            )


async def test_document_workflow_happy_path() -> None:
    """Extract runs first, its result feeds synthesis, the outcome is returned."""
    _reset()
    config = DocTTSConfig(
        source_path="/data/doc_tts/uploads/doc-tts-test.epub",
        filename="clase.epub",
        title="Clase de espanol",
        voice="es-ES-ElviraNeural",
        language="es",
    )

    result = await _run(config)

    assert result["status"] == "ok"
    assert result["run_id"] == "doc-tts-test"
    assert result["audio_path"] == SYNTHESIZED["audio_path"]
    assert result["chunk_count"] == 3

    assert len(EXTRACT_CALLS) == 1
    extract = EXTRACT_CALLS[0]
    assert extract["run_id"] == "doc-tts-test", "the id comes from workflow.info()"
    assert extract["source_path"].endswith("doc-tts-test.epub")
    assert extract["filename"] == "clase.epub"

    assert len(SYNTH_CALLS) == 1
    synthesize = SYNTH_CALLS[0]
    assert synthesize["text_path"] == EXTRACTED["text_path"], "extract result must feed synth"
    assert synthesize["title"] == EXTRACTED["title"]
    assert synthesize["voice"] == "es-ES-ElviraNeural"
    assert synthesize["run_id"] == "doc-tts-test"


async def test_document_workflow_extraction_failure_stops_before_synthesis() -> None:
    """An unreadable document fails the run and never reaches the TTS engine."""
    _reset()
    config = DocTTSConfig(source_path="/data/x.pdf", filename="broken.pdf")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq-doc-fail",
            workflows=[DocumentTtsWorkflow],
            activities=[stub_extract, stub_synthesize],
        ):
            with pytest.raises(WorkflowFailureError) as excinfo:
                await env.client.execute_workflow(
                    DocumentTtsWorkflow.run,
                    config,
                    id="doc-tts-fail",
                    task_queue="tq-doc-fail",
                )

    assert "Document extraction failed" in str(excinfo.value.cause)
    assert SYNTH_CALLS == [], "no synthesis after a failed extraction"


async def test_document_workflow_defaults_are_empty_overrides() -> None:
    """Missing voice/title/language leave the services to use their defaults."""
    _reset()
    config = DocTTSConfig(source_path="/data/nota.txt", filename="nota.txt")

    await _run(config, workflow_id="doc-tts-defaults")

    assert SYNTH_CALLS[0]["voice"] == ""
    assert SYNTH_CALLS[0]["language"] == ""
    assert EXTRACT_CALLS[0]["title"] == ""
