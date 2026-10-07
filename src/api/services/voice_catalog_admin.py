"""Catalog administration shared by REST and GUI (Phase 9.3).

The catalog endpoints of ``/api/admin/voices`` and the voice cards of
``/ui/voices`` describe the same actions (inspect statuses, queue a design
run, create a custom voice), so the logic lives here once and the routes
only translate HTTP to service calls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from src.api.models.voices import (
    CustomVoiceIn,
    VoiceManifestEntry,
    VoiceProfile,
    is_voice_slug,
)
from src.core.config import settings
from src.core.voice_catalog import (
    MANIFEST_NAME,
    REFS_DIR,
    VoiceCatalogError,
    is_stale,
    load_custom_profiles,
    load_manifest,
    load_profiles,
    missing_or_stale,
    save_custom_profile,
)
from src.workers.run_worker import build_client
from src.workers.voice_workflows import VoiceCatalogConfig, VoiceCatalogWorkflow

logger = structlog.get_logger(__name__)

#: Statuses of a catalog voice's reference audio, in Spanish as shown in the GUI.
STATUS_PENDING = "pendiente"
STATUS_GENERATED = "generada"
STATUS_STALE = "desactualizada"

#: Badge CSS class per status (``base.html`` badge styles).
STATUS_BADGES = {
    STATUS_PENDING: "running",
    STATUS_GENERATED: "ok",
    STATUS_STALE: "terminated",
}


class VoiceCatalogAdminError(RuntimeError):
    """A catalog administration action could not be completed."""


def manifest_path() -> Path:
    """Return the manifest path under the configured voices directory.

    Returns:
        Absolute-or-relative path of ``manifest.json`` in ``settings.voices_dir``.
    """
    return Path(settings.voices_dir) / MANIFEST_NAME


def refs_dir() -> Path:
    """Return the references directory under the configured voices directory.

    Returns:
        Path of the ``refs`` directory in ``settings.voices_dir``.
    """
    return Path(settings.voices_dir) / REFS_DIR


def reference_wav(name: str) -> Path | None:
    """Resolve one profile's reference WAV safely (used by audio + preview).

    Args:
        name: Profile name, optionally with a ``.wav`` suffix.

    Returns:
        The resolved WAV path, or None when the name is unsafe, the profile
        is unknown, the manifest entry escapes the voices directory, or the
        file does not exist.
    """
    stem = name[:-4] if name.lower().endswith(".wav") else name
    if not is_voice_slug(stem):
        return None
    try:
        profile = next((p for p in load_profiles() if p.name == stem), None)
    except VoiceCatalogError:
        return None
    if profile is None:
        return None
    voices_root = Path(settings.voices_dir).resolve()
    entry = load_manifest(manifest_path()).get(profile.name)
    relative = entry.file if entry is not None else f"{REFS_DIR}/{profile.name}.wav"
    wav = (voices_root / relative).resolve()
    if not wav.is_relative_to(voices_root) or not wav.is_file():
        return None
    return wav


def generated_status(
    profile: VoiceProfile,
    manifest: dict[str, VoiceManifestEntry],
    voices_dir: Path | None = None,
) -> str:
    """Classify one profile's reference audio for the GUI.

    A voice passes to "generada" as soon as its WAV exists on disk (the
    decision the user took for Phase 9); a manifest entry that no longer
    matches the profile is "desactualizada" and a profile with no usable
    reference is "pendiente".

    Args:
        profile: Profile to classify.
        manifest: Loaded manifest (missing entries mean not generated).
        voices_dir: Voices directory; defaults to ``settings.voices_dir``.

    Returns:
        One of ``pendiente``, ``generada`` or ``desactualizada``.
    """
    entry = manifest.get(profile.name)
    if entry is None:
        return STATUS_PENDING
    if is_stale(profile, entry):
        return STATUS_STALE
    root = Path(voices_dir) if voices_dir is not None else Path(settings.voices_dir)
    if not (root / entry.file).is_file():
        return STATUS_PENDING
    return STATUS_GENERATED


async def catalog_status() -> dict[str, Any]:
    """Return the catalog with its generation status.

    Returns:
        Dict with the profiles and which are generated, missing or stale.
    """
    profiles = load_profiles()
    manifest = load_manifest(manifest_path())
    pending = set(missing_or_stale(profiles, manifest))
    custom_names = {profile.name for profile in _safe_custom_names()}
    return {
        "status": "ok",
        "profiles": [profile.model_dump(mode="json") for profile in profiles],
        "generated": sorted(p.name for p in profiles if p.name not in pending and p.enabled),
        "missing": sorted(pending),
        "stale": sorted(pending),
        "custom": sorted(custom_names),
    }


def _safe_custom_names() -> list[VoiceProfile]:
    """Load the custom profiles, tolerating an unreadable file for listings."""
    try:
        return load_custom_profiles()
    except VoiceCatalogError as exc:
        logger.warning("custom_voice_profiles_unreadable", error=str(exc))
        return []


async def generated_voices() -> list[dict[str, Any]]:
    """Build the rows of the "Voces generadas" card.

    Returns:
        One dict per catalog voice with ``profile``, ``custom``, ``status``
        and ``badge`` keys, custom voices first.
    """
    profiles = load_profiles()
    manifest = load_manifest(manifest_path())
    custom_names = {profile.name for profile in _safe_custom_names()}
    root = Path(settings.voices_dir)
    rows: list[dict[str, Any]] = []
    for profile in profiles:
        status = generated_status(profile, manifest, root)
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


async def start_generate(names: list[str] | None = None, upload: bool = False) -> str:
    """Start a voice catalog generation run in Temporal.

    Args:
        names: Optional subset of profile names; empty means the whole catalog.
        upload: Also upload the produced references to the Modal Volume.

    Returns:
        The workflow id.

    Raises:
        VoiceCatalogAdminError: If the workflow cannot be started.
    """
    workflow_id = f"voice-catalog-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
    config = VoiceCatalogConfig(
        names=list(names or []),
        upload=upload,
        task_queue=settings.voice_catalog_task_queue,
        design_task_queue=settings.voice_design_task_queue,
    )
    try:
        client = await build_client(settings.temporal_voices_namespace)
        await client.execute_workflow(
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
        VoiceCatalogError: If the name is already taken or the existing
            custom file cannot be read.
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
    save_custom_profile(profile)

    workflow_id = ""
    start_error = ""
    if payload.generate:
        try:
            workflow_id = await start_generate(names=[profile.name], upload=payload.upload)
        except VoiceCatalogAdminError as exc:
            start_error = str(exc)
    return {"profile": profile, "workflow_id": workflow_id, "start_error": start_error}
