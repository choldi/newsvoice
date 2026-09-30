"""Temporal activities for the voice catalog (Phase 7.8).

These activities run on the Modal narration worker: they hold the Modal
credentials, generate reference voices with VoiceDesign, store them locally
(the source of truth) and upload them to the Modal Volume for cloning.

Everything with side effects lives here; the workflow only orchestrates.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from src.api.models.voices import VoiceManifestEntry
from src.core.config import settings
from src.core.voice_catalog import (
    load_manifest,
    load_profiles,
    missing_or_stale,
    sha256_bytes,
    sha256_text,
    write_manifest,
)

#: ``audio_jobs.kind`` discriminator for voice-catalog jobs.
VOICE_JOB_KIND = "voice"


def _profile(name: str) -> dict[str, Any]:
    """Return the catalog profile matching a name.

    Args:
        name: Profile name.

    Returns:
        The profile as a dict.

    Raises:
        ApplicationError: When no profile has that name. Non-retryable.
    """
    for profile in load_profiles():
        if profile.name == name:
            return profile.model_dump(mode="json")
    raise ApplicationError(f"unknown voice profile: {name}", non_retryable=True)


@activity.defn
async def design_voice_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Generate a reference voice sample on Modal and store it locally.

    The reference is written to ``data/voices/refs/<name>.wav`` and recorded
    in the manifest with the digests that detect profile drift.

    Args:
        payload: Dict with ``name``, ``instruct``, ``ref_text``, ``language``.

    Returns:
        Dict with ``name``, ``file``, ``sha256``, ``sample_rate`` and
        ``duration_seconds``.

    Raises:
        ApplicationError: When the payload is invalid or the Modal call fails.
    """
    name = str(payload.get("name") or "")
    instruct = str(payload.get("instruct") or "")
    ref_text = str(payload.get("ref_text") or "")
    language = str(payload.get("language") or "en")
    if not name or not instruct or not ref_text:
        raise ApplicationError(
            "design_voice_activity needs name, instruct and ref_text",
            non_retryable=True,
        )

    from src.api.services.narration_tts import wav_duration
    from src.core.modal_client import modal_design_voice

    activity.logger.info("Designing voice", name=name, language=language)
    started = time.monotonic()
    try:
        wav, sample_rate = await modal_design_voice(
            text=ref_text, instruct=instruct, language=language
        )
    except Exception as exc:
        raise ApplicationError(f"voice design failed for {name}: {exc}") from exc

    voices_dir = Path(settings.voices_dir)
    refs_dir = voices_dir / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    ref_path = refs_dir / f"{name}.wav"
    ref_path.write_bytes(wav)

    entry = VoiceManifestEntry(
        file=f"refs/{name}.wav",
        sha256=sha256_bytes(wav),
        sample_rate=sample_rate,
        duration_seconds=wav_duration(wav),
        design_model=settings.qwen_tts_design_model,
        instruct_sha256=sha256_text(instruct),
        ref_text_sha256=sha256_text(ref_text),
        created_at=datetime.now(UTC).isoformat(),
        modal_cost_usd=round(time.monotonic() - started, 2) * settings.modal_usd_per_gpu_second,
    )
    manifest_path = voices_dir / "manifest.json"
    manifest = load_manifest(manifest_path)
    manifest[name] = entry
    write_manifest(manifest, manifest_path)

    activity.logger.info(
        "Voice designed",
        name=name,
        duration_seconds=entry.duration_seconds,
        sha256=entry.sha256,
    )
    return {
        "name": name,
        "file": entry.file,
        "sha256": entry.sha256,
        "sample_rate": entry.sample_rate,
        "duration_seconds": entry.duration_seconds,
    }


@activity.defn
async def upload_voice_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Upload a reference voice to the Modal Volume for cloning.

    Args:
        payload: Dict with ``name``.

    Returns:
        Dict with ``name`` and ``uploaded``.

    Raises:
        ApplicationError: When the voice is missing or the upload fails.
    """
    name = str(payload.get("name") or "")
    if not name:
        raise ApplicationError("upload_voice_activity needs a name", non_retryable=True)

    from src.core.modal_client import upload_voice

    ref_path = Path(settings.voices_dir) / "refs" / f"{name}.wav"
    if not ref_path.is_file():
        raise ApplicationError(f"voice '{name}' has no reference at {ref_path}", non_retryable=True)
    try:
        await upload_voice(name, ref_path.read_bytes())
    except Exception as exc:
        raise ApplicationError(f"voice upload failed for {name}: {exc}") from exc
    activity.logger.info("Voice uploaded", name=name)
    return {"name": name, "uploaded": True}


@activity.defn
async def voice_catalog_status_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Report which catalog voices are generated, missing or stale.

    Reads the catalog and the manifest so the workflow stays deterministic: a
    workflow must not do file I/O.

    Args:
        payload: Dict with ``names`` (optional subset of profile names).

    Returns:
        Dict with ``generated``, ``missing``, ``stale``, ``pending`` and
        ``profiles`` (the full profile dicts for pending names).
    """
    names = [str(name) for name in (payload.get("names") or [])]
    profiles = load_profiles()
    by_name = {profile.name: profile for profile in profiles}
    targets = [by_name[name] for name in names if name in by_name] if names else profiles
    manifest = load_manifest()
    pending = set(missing_or_stale(targets, manifest))
    return {
        "generated": sorted(p.name for p in targets if p.name not in pending and p.enabled),
        "missing": sorted(pending),
        "stale": sorted(pending),
        "pending": sorted(pending),
        "profiles": [p.model_dump(mode="json") for p in targets if p.name in pending],
    }
