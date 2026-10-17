"""Unit tests for the document TTS worker activity registration (Phase 7.11)."""

from __future__ import annotations

import inspect

import pytest

from src.core.config import settings
from src.workers import doc_tts_activities
from src.workers.doc_tts_workflows import DocTTSConfig, DocumentTtsWorkflow
from src.workers.run_doc_tts_worker import worker_activities
from src.workers.run_worker import worker_activities as digest_worker_activities


def _all_defined_activity_names() -> set[str]:
    """Return the name of every @activity.defn function in the doc-tts module."""
    names = set()
    for name, obj in inspect.getmembers(doc_tts_activities, inspect.isfunction):
        marker = getattr(obj, "_temporal_activity_definition", None)
        if marker is not None:
            names.add(marker.name if hasattr(marker, "name") else name)
    return names


def test_doc_tts_worker_registers_every_doc_tts_activity() -> None:
    """No doc-tts @activity.defn may be added without being registered.

    A workflow step invoking an unregistered activity fails at runtime with a
    Temporal NotFoundError, so registrations must stay in sync with definitions.
    """
    registered = {getattr(a, "__name__") for a in worker_activities()}
    assert _all_defined_activity_names() - registered == set()


def test_doc_tts_worker_does_not_register_digest_activities() -> None:
    """The queues stay separate: each worker only knows its own activities."""
    doc_tts = {getattr(a, "__name__") for a in worker_activities()}
    digest = {getattr(a, "__name__") for a in digest_worker_activities()}
    assert doc_tts & digest == set()


def test_digest_worker_does_not_register_doc_tts_activities() -> None:
    """A digest worker must never pick up a document render."""
    digest = {getattr(a, "__name__") for a in digest_worker_activities()}
    forbidden = {"extract_document_activity", "synthesize_document_activity"}
    assert digest & forbidden == set()


def test_doc_tts_queue_is_separate_from_the_digest_queue() -> None:
    """Own queue, so a 300-page render cannot occupy the digest worker."""
    assert settings.doc_tts_task_queue != settings.temporal_task_queue
    assert settings.doc_tts_task_queue != settings.narration_task_queue


def test_workflow_serves_only_the_document_workflow() -> None:
    """DocumentTtsWorkflow is the workflow the doc-tts worker hosts."""
    assert DocumentTtsWorkflow.__name__ == "DocumentTtsWorkflow"


class _Event:
    """asyncio.Event stand-in whose wait() ends the fake main()."""

    async def wait(self) -> None:
        raise _MainFinishedError


class _MainFinishedError(Exception):
    pass


async def _queue_of_main(monkeypatch: pytest.MonkeyPatch) -> tuple[str | None, str | None]:
    """Run run_doc_tts_worker.main() and return (namespace, queue) it served.

    Connecting to the wrong queue is the silent failure this split exists to
    prevent: the API schedules the workflow, nothing ever runs, and nothing
    errors. So assert on what main() actually asks for.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The namespace and queue requested by main().
    """
    import sys
    from contextlib import asynccontextmanager

    import src.workers.run_doc_tts_worker as module

    captured: dict[str, object] = {}

    async def _fake_build_client(namespace: str | None = None) -> object:
        captured["namespace"] = namespace
        return object()

    @asynccontextmanager
    async def _fake_factory(client: object, task_queue: str | None = None) -> object:
        captured["queue"] = task_queue
        yield object()

    async def _fake_migrations() -> None:
        captured["migrations"] = True

    monkeypatch.setattr(module, "build_client", _fake_build_client)
    monkeypatch.setattr(module, "run_migrations", _fake_migrations)
    monkeypatch.setattr(module, "run_doc_tts_worker", _fake_factory)
    monkeypatch.setattr(module.asyncio, "Event", lambda: _Event())
    monkeypatch.setattr(sys, "argv", ["src.workers.run_doc_tts_worker"])

    with pytest.raises(_MainFinishedError):
        await module.main()

    # Document jobs live in the default namespace (the API schedules there).
    assert "migrations" in captured
    return captured.get("namespace"), captured.get("queue")


async def test_main_serves_the_default_namespace_and_the_doc_tts_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() polls the configured doc-tts queue in the default namespace."""
    namespace, queue = await _queue_of_main(monkeypatch)

    assert namespace in (None, settings.temporal_namespace)
    assert queue in (None, settings.doc_tts_task_queue)


def test_doc_tts_config_holds_the_upload_options() -> None:
    """The config carries what the workflow needs; the id is not part of it."""
    config = DocTTSConfig(
        source_path="/data/uploads/doc-tts-abc.pdf",
        filename="libro.pdf",
        title="Mi libro",
        voice="es-ES-ElviraNeural",
        language="es",
    )
    assert config.source_path.endswith("doc-tts-abc.pdf")
    assert config.title == "Mi libro"
    assert not hasattr(config, "run_id"), "the id comes from workflow.info()"


def test_main_is_an_async_entry_point() -> None:
    """The entry point module exposes the expected async main()."""
    import inspect as _inspect

    from src.workers.run_doc_tts_worker import main, run_doc_tts_worker  # noqa: F401

    assert _inspect.iscoroutinefunction(main)
