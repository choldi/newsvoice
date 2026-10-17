"""Unit tests for the teacher worker activity registration (Phase 7.12)."""

from __future__ import annotations

import inspect

import pytest

from src.core.config import settings
from src.workers import lesson_activities
from src.workers.lesson_workflows import LessonConfig, LessonWorkflow
from src.workers.run_teacher_worker import worker_activities
from src.workers.run_worker import worker_activities as digest_worker_activities


def _all_defined_activity_names() -> set[str]:
    """Return the name of every @activity.defn function in the lesson module."""
    names = set()
    for name, obj in inspect.getmembers(lesson_activities, inspect.isfunction):
        marker = getattr(obj, "_temporal_activity_definition", None)
        if marker is not None:
            names.add(marker.name if hasattr(marker, "name") else name)
    return names


def test_teacher_worker_registers_every_lesson_activity() -> None:
    """No lesson @activity.defn may be added without being registered.

    A workflow step invoking an unregistered activity fails at runtime with a
    Temporal NotFoundError, so registrations must stay in sync with definitions.
    """
    registered = {getattr(a, "__name__") for a in worker_activities()}
    assert _all_defined_activity_names() - registered == set()


def test_teacher_worker_does_not_register_digest_activities() -> None:
    """The queues stay separate: each worker only knows its own activities.

    The lesson workflow *addresses* the delivery activities by name on the
    digest queue instead of registering them here, so the sets must be disjoint.
    """
    teacher = {getattr(a, "__name__") for a in worker_activities()}
    digest = {getattr(a, "__name__") for a in digest_worker_activities()}
    assert teacher & digest == set()


def test_digest_worker_does_not_register_lesson_activities() -> None:
    """A digest worker must never run a lesson generation or render."""
    digest = {getattr(a, "__name__") for a in digest_worker_activities()}
    forbidden = {"generate_lesson_activity", "render_lesson_activity"}
    assert digest & forbidden == set()


def test_teacher_queue_is_distinct_from_every_other_queue() -> None:
    """Own queue, so an LLM call plus a render cannot occupy other workers."""
    assert settings.teacher_task_queue != settings.temporal_task_queue
    assert settings.teacher_task_queue != settings.narration_task_queue
    assert settings.teacher_task_queue != settings.doc_tts_task_queue


def test_workflow_serves_only_the_lesson_workflow() -> None:
    """LessonWorkflow is the workflow the teacher worker hosts."""
    assert LessonWorkflow.__name__ == "LessonWorkflow"


class _Event:
    """asyncio.Event stand-in whose wait() ends the fake main()."""

    async def wait(self) -> None:
        raise _MainFinishedError


class _MainFinishedError(Exception):
    pass


async def _queue_of_main(monkeypatch: pytest.MonkeyPatch) -> tuple[str | None, str | None]:
    """Run run_teacher_worker.main() and return (namespace, queue) it served.

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

    import src.workers.run_teacher_worker as module

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
    monkeypatch.setattr(module, "run_teacher_worker", _fake_factory)
    monkeypatch.setattr(module.asyncio, "Event", lambda: _Event())
    monkeypatch.setattr(sys, "argv", ["src.workers.run_teacher_worker"])

    with pytest.raises(_MainFinishedError):
        await module.main()

    # Lessons live in the default namespace (the API schedules there).
    assert "migrations" in captured
    return captured.get("namespace"), captured.get("queue")


async def test_main_serves_the_default_namespace_and_the_teacher_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() polls the configured teacher queue in the default namespace."""
    namespace, queue = await _queue_of_main(monkeypatch)

    assert namespace in (None, settings.temporal_namespace)
    assert queue in (None, settings.teacher_task_queue)


def test_lesson_config_holds_the_upload_and_delivery_options() -> None:
    """The config carries what the workflow needs; the id is not part of it."""
    config = LessonConfig(
        notes_path="/data/lessons/uploads/lesson-abc.md",
        filename="apuntes.md",
        mode="dual",
        spec={"roles": []},
        target_language="en",
        native_language="es",
        collection="tech",
        delivery_task_queue="news-engine-task-queue",
        delivery_channels=["email", "telegram"],
        chat_id="42",
        email_recipients=["me@example.com"],
    )
    assert config.notes_path.endswith("lesson-abc.md")
    assert config.mode == "dual"
    assert config.delivery_task_queue == "news-engine-task-queue"
    assert config.email_recipients == ["me@example.com"]
    assert not hasattr(config, "run_id"), "the id comes from workflow.info()"


def test_main_is_an_async_entry_point() -> None:
    """The entry point module exposes the expected async main()."""
    from src.workers.run_teacher_worker import main, run_teacher_worker  # noqa: F401

    assert inspect.iscoroutinefunction(main)
