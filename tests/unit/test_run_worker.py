"""Unit tests for the Temporal worker activity registration."""

import inspect

from src.workers import activities as activities_module
from src.workers.run_worker import worker_activities


def _all_defined_activity_names() -> set[str]:
    """Return the name of every @activity.defn function in src.workers.activities."""
    names = set()
    for name, obj in inspect.getmembers(activities_module, inspect.isfunction):
        marker = getattr(obj, "_temporal_activity_definition", None)
        if marker is not None:
            names.add(marker.name if hasattr(marker, "name") else name)
    return names


def test_worker_registers_every_temporal_activity() -> None:
    """No @activity.defn may be added without being registered on the worker.

    A workflow step invoking an unregistered activity fails at runtime with a
    Temporal NotFoundError (seen live for the WhatsApp and audio-compression
    activities), so registrations must stay in sync with the definitions.
    """
    registered = {getattr(a, "__name__") for a in worker_activities()}
    missing = _all_defined_activity_names() - registered
    assert missing == set()


def test_worker_activities_include_whatsapp_and_compress() -> None:
    """WhatsApp and audio-compression activities are registered on the worker."""
    registered = {getattr(a, "__name__") for a in worker_activities()}
    for expected in (
        "deliver_whatsapp_message_activity",
        "deliver_whatsapp_audio_activity",
        "compress_audio_activity",
    ):
        assert expected in registered
