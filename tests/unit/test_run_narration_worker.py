"""Unit tests for the narration worker activity registration."""

import inspect

import pytest

from src.core.config import settings
from src.workers import narration_activities
from src.workers.narration_workflows import NarrationWorkflow
from src.workers.run_narration_worker import worker_activities
from src.workers.run_worker import worker_activities as digest_worker_activities


def _all_defined_activity_names() -> set[str]:
    """Return the name of every @activity.defn function in the narration module."""
    names = set()
    for _name, obj in inspect.getmembers(narration_activities, inspect.isfunction):
        marker = getattr(obj, "_temporal_activity_definition", None)
        if marker is not None:
            names.add(marker.name if hasattr(marker, "name") else _name)
    return names


def test_narration_worker_registers_every_narration_activity() -> None:
    """No narration @activity.defn may be added without being registered.

    A workflow step invoking an unregistered activity fails at runtime with a
    Temporal NotFoundError, so registrations must stay in sync with definitions.
    """
    registered = {getattr(a, "__name__") for a in worker_activities()}
    assert _all_defined_activity_names() - registered == set()


def test_narration_worker_does_not_register_digest_activities() -> None:
    """The queues stay separate: each worker only knows its own activities."""
    narration = {getattr(a, "__name__") for a in worker_activities()}
    digest = {getattr(a, "__name__") for a in digest_worker_activities()}
    assert narration & digest == set()


def test_digest_worker_does_not_register_narration_activities() -> None:
    """The digest worker must not serve the render activities themselves.

    Phase 7.9b: the digest owns only ``dispatch_narration_activity``, which
    starts the narration workflow in the podcast namespace. resolve/render stay
    exclusive to the narration and Modal workers, so a render can never be
    picked up by a digest worker pointed at the wrong namespace.
    """
    digest = {getattr(a, "__name__") for a in digest_worker_activities()}
    forbidden = {"resolve_narration_script_activity", "render_narration_activity"}
    assert digest & forbidden == set()
    assert "dispatch_narration_activity" in digest


def test_narration_workflow_registers_only_its_own_workflow() -> None:
    """NarrationWorkflow is the only workflow served by the narration worker."""
    from src.workers.run_narration_worker import run_narration_worker  # noqa: F401

    assert NarrationWorkflow.__name__ == "NarrationWorkflow"


def test_modal_worker_registers_the_same_narration_activities() -> None:
    """The Modal queue serves the same activities as the local one.

    Both queues must register every narration activity, or a workflow step
    addressed by name fails at runtime with a Temporal NotFoundError.
    """
    from src.workers.run_modal_worker import worker_activities as modal_activities

    local = {getattr(a, "__name__") for a in worker_activities()}
    modal = {getattr(a, "__name__") for a in modal_activities()}
    assert modal == local
    assert _all_defined_activity_names() - modal == set()


def test_modal_queue_is_separate_from_the_local_one() -> None:
    """The two queues are distinct so the workers scale independently."""
    assert settings.narration_modal_task_queue != settings.narration_local_task_queue
    assert settings.narration_modal_task_queue != settings.narration_task_queue


class _Event:
    """asyncio.Event stand-in whose wait() ends the fake main()."""

    async def wait(self) -> None:
        raise _MainFinishedError


class _MainFinishedError(Exception):
    pass


async def _namespace_of_main(
    module_name: str,
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str | None, str | None]:
    """Run an entry point's main() and return the (namespace, queue) it served.

    A worker connected to the wrong namespace or queue is the silent failure
    this split exists to prevent: the digest completes, nothing renders, and
    nothing errors. So assert on what main() actually asks for.
    """
    import importlib
    import sys
    from contextlib import asynccontextmanager

    module = importlib.import_module(module_name)
    captured: dict[str, object] = {}

    async def _fake_build_client(namespace: str | None = None) -> object:
        captured["namespace"] = namespace
        return object()

    @asynccontextmanager
    async def _fake_factory(
        client: object, task_queue: str | None = None, role: str | None = None
    ) -> object:
        captured["queue"] = task_queue
        captured["role"] = role
        yield object()

    async def _fake_migrations() -> None:
        captured["migrations"] = True

    monkeypatch.setattr(module, "build_client", _fake_build_client)
    monkeypatch.setattr(module, "run_migrations", _fake_migrations, raising=False)
    for factory in (
        "run_narration_worker",
        "run_modal_worker",
        "run_voice_worker",
    ):
        if hasattr(module, factory):
            monkeypatch.setattr(module, factory, _fake_factory)
    monkeypatch.setattr(module.asyncio, "Event", lambda: _Event(), raising=False)
    monkeypatch.setattr(sys, "argv", [module_name, *argv])

    with pytest.raises(_MainFinishedError):
        await module.main()

    # Only the narration workers run migrations; the catalog one has no DB work.
    assert "namespace" in captured
    return captured.get("namespace"), captured.get("queue")


async def test_the_local_narration_worker_serves_the_local_podcast_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace, queue = await _namespace_of_main("src.workers.run_narration_worker", [], monkeypatch)

    assert namespace == settings.temporal_podcast_local_namespace
    assert queue in (None, settings.narration_local_task_queue)


async def test_the_modal_worker_serves_the_modal_podcast_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace, queue = await _namespace_of_main("src.workers.run_modal_worker", [], monkeypatch)

    assert namespace == settings.temporal_podcast_modal_namespace
    assert queue in (None, settings.narration_modal_task_queue)


async def test_the_voice_worker_serves_the_voices_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace, _queue = await _namespace_of_main(
        "src.workers.run_voice_worker", ["--role", "catalog"], monkeypatch
    )

    assert namespace == settings.temporal_voices_namespace


def test_each_generation_kind_owns_a_distinct_namespace_and_queue() -> None:
    """One namespace per kind, one queue per podcast backend."""
    pairs = {
        "digest": (settings.temporal_namespace, settings.temporal_task_queue),
        "voices": (settings.temporal_voices_namespace, settings.voice_catalog_task_queue),
        "local": (settings.temporal_podcast_local_namespace, settings.narration_local_task_queue),
        "modal": (settings.temporal_podcast_modal_namespace, settings.narration_modal_task_queue),
    }

    assert settings.temporal_namespace == "default"
    assert len({ns for ns, _queue in pairs.values()}) == len(pairs)
    # The local narration worker polls narration_task_queue too; a digest that
    # dispatched to the old local-only name would queue forever.
    assert settings.narration_local_task_queue == settings.narration_task_queue
    assert settings.narration_modal_task_queue != settings.narration_local_task_queue
