"""Catalog administration shared by REST and GUI (Phase 9.3).

The catalog endpoints of ``/api/admin/voices`` and the voice cards of
``/ui/voices`` describe the same actions (inspect statuses, queue a design
run, create a custom voice), so the logic lives here once and the routes
only translate HTTP to service calls.

Nothing here touches a catalog file: since P6 the gateway owns the profiles,
the manifest and the reference WAVs, and :mod:`src.api.services.voice_catalog_client`
is the only path to them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from tts_contract import (
    STATUS_GENERATED,
    STATUS_PENDING,
    STATUS_STALE,
    VoiceCatalogError,
    VoiceCatalogResponse,
)

from src.api.models.voices import (
    CustomVoiceIn,
    VoiceProfile,
    is_voice_slug,
)
from src.api.services.voice_catalog_client import (
    fetch_catalog,
    fetch_reference_audio,
    save_custom_profile,
)
from src.core.config import settings
from src.workers.run_worker import build_client
from src.workers.voice_workflows import VoiceCatalogConfig, VoiceCatalogWorkflow

logger = structlog.get_logger(__name__)

#: Badge CSS class per status (``base.html`` badge styles).
STATUS_BADGES = {
    STATUS_PENDING: "running",
    STATUS_GENERATED: "ok",
    STATUS_STALE: "terminated",
}


class VoiceCatalogAdminError(RuntimeError):
    """A catalog administration action could not be completed."""


async def reference_audio(name: str) -> bytes | None:
    """Return one profile's reference WAV (used by audio + preview).

    The profile must exist in the catalog, so an unknown name never reaches
    the gateway's reference endpoint.

    Args:
        name: Profile name, optionally with a ``.wav`` suffix.

    Returns:
        The WAV bytes, or None when the name is unsafe, the profile is unknown
        or the reference has not been generated.
    """
    stem = _stem(name)
    if not is_voice_slug(stem):
        return None
    try:
        snapshot = await fetch_catalog()
        if stem not in {profile.name for profile in snapshot.profiles}:
            return None
        return await fetch_reference_audio(stem)
    except VoiceCatalogError:
        logger.warning("voice_reference_unreadable", voice=name)
        return None


def _stem(name: str) -> str:
    """Strip the optional ``.wav`` suffix the audio routes append.

    Args:
        name: Profile name, with or without the suffix.

    Returns:
        The bare profile name.
    """
    return name[:-4] if name.lower().endswith(".wav") else name


async def catalog_status() -> VoiceCatalogResponse:
    """Return the catalog with its generation status.

    Always asks the gateway for a fresh payload: this is what the admin page
    polls after queueing a design run.

    Returns:
        Profiles, manifest, per-profile status and the generated/missing name
        lists.

    Raises:
        VoiceCatalogError: When the gateway refuses or cannot be reached.
    """
    return await fetch_catalog(max_age=0)


async def generated_voices() -> list[dict[str, Any]]:
    """Build the rows of the "Voces generadas" card.

    Returns:
        One dict per catalog voice with ``profile``, ``custom``, ``status``
        and ``badge`` keys, custom voices first.
    """
    snapshot = await fetch_catalog()
    custom_names = set(snapshot.custom)
    rows: list[dict[str, Any]] = []
    for profile in snapshot.profiles:
        status = snapshot.statuses.get(profile.name, STATUS_PENDING)
        rows.append(
            {
                "profile": profile,
                "custom": profile.name in custom_names,
                "status": status,
                "badge": STATUS_BADGES[status],
            }
        )
    rows.sort(key=lambda row: (not row["custom"], row["profile"].name))
    return rows


async def start_generate(
    names: list[str] | None = None,
    upload: bool = False,
    run_in_modal: bool = False,
) -> str:
    """Start a voice catalog generation run in Temporal.

    The run is enqueued fire-and-forget: the workflow id is returned as soon as
    Temporal accepts it, without waiting for the design activities to finish.
    Callers poll the catalog status for progress.

    Args:
        names: Optional subset of profile names; empty means the whole catalog.
        upload: Also upload the produced references to the Modal Volume.
        run_in_modal: Render the designs on Modal instead of the local GPU
            worker (Phase 10.4; needs ``MODAL_ENABLED`` + credentials).

    Returns:
        The workflow id.

    Raises:
        VoiceCatalogAdminError: If the workflow cannot be started.
    """
    workflow_id = f"voice-catalog-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
    config = VoiceCatalogConfig(
        names=list(names or []),
        upload=upload,
        run_in_modal=run_in_modal,
        task_queue=settings.voice_catalog_task_queue,
        design_task_queue=settings.voice_design_task_queue,
    )
    try:
        client = await build_client(settings.temporal_voices_namespace)
        await client.start_workflow(
            VoiceCatalogWorkflow.run,
            config,
            id=workflow_id,
            task_queue=settings.voice_catalog_task_queue,
        )
    except Exception as exc:
        logger.error("voice_catalog_generate_failed", error=str(exc), exc_info=True)
        raise VoiceCatalogAdminError(f"Failed to start voice catalog: {exc}") from exc
    return workflow_id


async def create_custom_voice(payload: CustomVoiceIn) -> dict[str, Any]:
    """Validate and save a custom voice profile, then queue its design run.

    The profile is persisted first: a Temporal outage must not lose the
    operator's work, so a failed enqueue comes back as ``start_error`` with
    the profile already saved.

    Args:
        payload: Custom voice request from REST or the GUI form.

    Returns:
        Dict with ``profile``, ``workflow_id`` (empty when not started or
        failed) and ``start_error`` (empty on success).

    Raises:
        ValidationError: If the payload is not a valid profile.
        VoiceCatalogError: If the gateway is unreachable or the name is
            already taken (the gateway's message is kept).
    """
    segments = [str(part).strip() for part in payload.ref_segments if str(part).strip()]
    if not segments and payload.ref_text.strip():
        segments = [payload.ref_text.strip()]
    profile = VoiceProfile.model_validate(
        {
            "name": payload.name,
            "label": payload.label.strip() or payload.name,
            "language": payload.language,
            "instruct": payload.instruct,
            "ref_segments": segments,
            "role_hint": payload.role_hint.strip(),
            "enabled": True,
        }
    )
    await save_custom_profile(profile)

    workflow_id = ""
    start_error = ""
    if payload.generate:
        try:
            workflow_id = await start_generate(
                names=[profile.name],
                upload=payload.upload,
                run_in_modal=payload.run_in_modal,
            )
        except VoiceCatalogAdminError as exc:
            start_error = str(exc)
    return {"profile": profile, "workflow_id": workflow_id, "start_error": start_error}
