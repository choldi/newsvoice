"""Unit tests for the narration worker activity registration."""

import inspect

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
    """The digest worker must not import or serve the narration module."""
    digest = {getattr(a, "__name__") for a in digest_worker_activities()}
    assert not any(name.endswith("narration") or "narration" in name for name in digest)


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
