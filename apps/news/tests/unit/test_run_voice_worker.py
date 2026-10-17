"""Unit tests for the voice worker entry point (Phase 7.9)."""

import inspect

import pytest

from src.core.config import settings
from src.workers import voice_activities
from src.workers.run_voice_worker import ROLES, registration
from src.workers.voice_workflows import VoiceCatalogWorkflow


def _all_defined_activity_names() -> set[str]:
    """Return the name of every @activity.defn in src.workers.voice_activities."""
    names = set()
    for name, obj in inspect.getmembers(voice_activities, inspect.isfunction):
        marker = getattr(obj, "_temporal_activity_definition", None)
        if marker is not None:
            names.add(marker.name if hasattr(marker, "name") else name)
    return names


def test_voice_workers_register_every_voice_activity() -> None:
    """Every voice @activity.defn is served by exactly one of the two roles.

    A workflow step invoking an unregistered activity fails at runtime with a
    Temporal NotFoundError, so the two roles must cover the whole module.
    """
    registered: set[str] = set()
    for role in ROLES:
        _, _, activities = registration(role)
        registered |= {getattr(a, "__name__") for a in activities}
    assert _all_defined_activity_names() - registered == set()


def test_design_role_registers_only_the_design_activity() -> None:
    """The GPU box synthesises and nothing else: no disk, no Modal, no workflow."""
    _, workflows, activities = registration("design")

    assert workflows == []
    assert [getattr(a, "__name__") for a in activities] == ["design_voice_activity"]
    assert all(a.__module__ == voice_activities.__name__ for a in activities)


def test_catalog_role_registers_the_workflow_and_the_store_side() -> None:
    """The server hosts the workflow and every activity that touches its disk."""
    _, workflows, activities = registration("catalog")

    assert workflows == [VoiceCatalogWorkflow]
    assert [getattr(a, "__name__") for a in activities] == [
        "voice_catalog_status_activity",
        "store_voice_activity",
        "upload_voice_activity",
    ]


def test_roles_use_distinct_queues() -> None:
    """Each role polls its own queue so the two boxes can scale apart."""
    design_queue, _, _ = registration("design")
    catalog_queue, _, _ = registration("catalog")

    assert design_queue == settings.voice_design_task_queue
    assert catalog_queue == settings.voice_catalog_task_queue
    assert design_queue != catalog_queue


def test_the_voices_namespace_is_its_own() -> None:
    """The voice flow is isolated from the digest pipeline's namespace."""
    assert settings.temporal_voices_namespace != settings.temporal_namespace


def test_unknown_role_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown voice worker role"):
        registration("render")
