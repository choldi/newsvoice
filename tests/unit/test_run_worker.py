"""Unit tests for the Temporal worker activity registration."""

import inspect

from src.workers import activities as activities_module
from src.workers.run_worker import worker_activities


def _all_deliver_activity_names() -> set[str]:
    """Return every deliver_* activity function defined in src.workers.activities."""
    names = set()
    for name, obj in inspect.getmembers(activities_module, inspect.isfunction):
        if name.startswith("deliver_") and name.endswith("_activity"):
            names.add(name)
    return names


def test_worker_registers_every_deliver_activity() -> None:
    """No deliver_*_activity may be added without being registered on the worker.

    A workflow step invoking an unregistered activity fails at runtime with a
    Temporal NotFoundError (seen live for the WhatsApp activities), so the
    registrations must stay in sync with the definitions.
    """
    registered = {getattr(a, "__name__") for a in worker_activities()}
    missing = _all_deliver_activity_names() - registered
    assert missing == set()


def test_worker_activities_include_whatsapp() -> None:
    """The WhatsApp text/audio activities are registered on the worker."""
    registered = {getattr(a, "__name__") for a in worker_activities()}
    assert "deliver_whatsapp_message_activity" in registered
    assert "deliver_whatsapp_audio_activity" in registered
