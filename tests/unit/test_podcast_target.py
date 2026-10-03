"""Unit tests for the podcast target selector (Phase 7.9b).

Podcast generation is split by backend: the Modal renders live in the Modal
namespace/queue and everything else in the local ones. The selector is the one
place that decides it, so a silent default change here would send renders to
the wrong namespace.
"""

from src.api.models.narration import NarrationSpec, TTSBackend
from src.core.config import settings
from src.workers.podcast_target import backend_from_spec, podcast_target

LOCAL_SPEC = NarrationSpec(
    title="Edicion",
    roles=[
        {"name": "anchor", "voice_id": "es-mx-male"},
        {"name": "guest", "voice_id": "es-mx-female"},
    ],
    pause_ms=400,
)

MODAL_SPEC = LOCAL_SPEC.model_copy(update={"tts_backend": TTSBackend.QWEN3_TTS})


def test_local_backend_targets_the_local_namespace_and_queue() -> None:
    target = podcast_target(LOCAL_SPEC.model_dump())

    assert target.namespace == settings.temporal_podcast_local_namespace
    assert target.task_queue == settings.narration_local_task_queue
    assert target.modal is False


def test_modal_backend_targets_the_modal_namespace_and_queue() -> None:
    target = podcast_target(MODAL_SPEC.model_dump())

    assert target.namespace == settings.temporal_podcast_modal_namespace
    assert target.task_queue == settings.narration_modal_task_queue
    assert target.modal is True


def test_zonos2_is_also_modal() -> None:
    """ZONOS2 renders on Modal too, so it must not land on the local side."""
    spec = LOCAL_SPEC.model_copy(update={"tts_backend": TTSBackend.ZONOS2})

    assert podcast_target(spec.model_dump()).modal is True


def test_a_missing_spec_renders_locally() -> None:
    """No spec means no Modal backend, so the local side is the safe default."""
    for spec in (None, {}, {"roles": []}):
        assert podcast_target(spec).modal is False


def test_backend_from_spec_defaults_to_the_local_backend() -> None:
    assert backend_from_spec(None) == "omni_voice"
    assert backend_from_spec({}) == "omni_voice"
    assert backend_from_spec({"tts_backend": "zonos2"}) == "zonos2"


def test_the_two_podcast_targets_never_share_namespace_or_queue() -> None:
    """A mix-up here would strand renders on a queue nobody polls."""
    local = podcast_target(LOCAL_SPEC.model_dump())
    modal = podcast_target(MODAL_SPEC.model_dump())

    assert local.namespace != modal.namespace
    assert local.task_queue != modal.task_queue


def test_the_three_generation_namespaces_are_distinct() -> None:
    """Voces, podcast local and podcast modal are three separate namespaces."""
    namespaces = {
        settings.temporal_voices_namespace,
        settings.temporal_podcast_local_namespace,
        settings.temporal_podcast_modal_namespace,
    }

    assert len(namespaces) == 3


def test_the_local_queue_alias_matches_the_worker_queue() -> None:
    """Phase 7.9b collapsed the two local queue names.

    ``narration_task_queue`` and ``narration_local_task_queue`` used to point
    at different queues with a single worker, so local digest narrations queued
    forever. They must stay in sync.
    """
    assert settings.narration_local_task_queue == settings.narration_task_queue
