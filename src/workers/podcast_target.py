"""Namespace and task-queue selection for podcast generation (Phase 7.9b).

A Temporal activity always executes in the namespace of the workflow that
scheduled it, so a render that must live in its own namespace cannot stay an
activity of the digest workflow: the digest hands the whole narration to a
``NarrationWorkflow`` started from a dispatch activity, and this module decides
*where* that workflow goes.

The decision is the same everywhere (schedule registration, admin route, CLI
and the dispatch activity), so it lives here once: Modal backends render on
the Modal queue inside the Modal namespace, everything else renders locally in
the local namespace. The backend is read from the serialized spec dict so this
helper stays importable from workflows, activities and routes alike, without
pulling the pydantic models in.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from src.core.config import settings


class PodcastTarget(NamedTuple):
    """Where one podcast render has to run.

    Attributes:
        namespace: Temporal namespace hosting the render.
        task_queue: Task queue the narration worker polls for that namespace.
        modal: Whether the target is the Modal side (GPU spend, credentials).
    """

    namespace: str
    task_queue: str
    modal: bool


def backend_from_spec(spec: dict[str, Any] | None) -> str:
    """Return the TTS backend a narration spec asks for.

    Args:
        spec: Serialized ``NarrationSpec`` payload, or None.

    Returns:
        The backend id, or ``omni_voice`` (local) when absent or unknown.
    """
    if not isinstance(spec, dict):
        return "omni_voice"
    return str(spec.get("tts_backend") or "omni_voice")


def podcast_target(spec: dict[str, Any] | None) -> PodcastTarget:
    """Resolve the namespace and task queue for a narration spec.

    Modal backends go to the Modal namespace and queue (the worker holding the
    Modal credentials); everything else — including the direct ``edge`` backend,
    which needs no gateway at all — stays local.

    Args:
        spec: Serialized ``NarrationSpec`` payload, or None.

    Returns:
        The PodcastTarget for this render.
    """
    if backend_from_spec(spec) not in ("omni_voice", "edge"):
        return PodcastTarget(
            namespace=settings.temporal_podcast_modal_namespace,
            task_queue=settings.narration_modal_task_queue,
            modal=True,
        )
    return PodcastTarget(
        namespace=settings.temporal_podcast_local_namespace,
        task_queue=settings.narration_local_task_queue,
        modal=False,
    )
