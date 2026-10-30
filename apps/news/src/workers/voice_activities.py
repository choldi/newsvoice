"""Temporal activities for the voice catalog (Phase 7.8/7.9).

The activities are split across two machines on purpose:

* ``design_voice_activity`` runs on the voice-design queue, polled by the GPU
  box. It loads Qwen3-TTS, synthesises the reference and ships the bytes to
  the server's staging endpoint (``/api/internal/voices/staged/{name}``):
  putting 24 voices through activity results filled the workflow history and
  the server terminated the run. The result carries only the digest. It never
  touches the server disk.
* ``store_voice_activity``, ``upload_voice_activity`` and
  ``voice_catalog_status_activity`` run on the narration queue, which ships
  the staged bytes to the gateway: since P6 the gateway owns ``refs/`` and the
  manifest, so this side uploads and reads them back over HTTP.

Everything with side effects lives here; the workflow only orchestrates.
"""

from __future__ import annotations

import asyncio
import time
import zlib
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from src.api.services.voice_catalog_client import (
    VoiceCatalogRequestError,
    fetch_catalog,
    fetch_reference_audio,
    store_reference,
)
from src.core.audio import AudioError, wav_duration, wav_format
from src.core.config import settings
from src.core.voice_catalog import (
    load_profiles,
    missing_or_stale,
    sha256_bytes,
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
    """Synthesise a reference voice on the GPU box and stage it for the server.

    The audio itself never enters the workflow: it goes straight to the
    staging endpoint, so the history only grows by the digest. The design
    worker has no access to ``data/voices``, which is why writing the real
    reference stays a separate activity.

    Args:
        payload: Dict with ``name``, ``instruct``, ``ref_text`` and
            ``language``. Optional ``ref_segments`` replaces ``ref_text`` as
            the list of segments to speak (they are joined into one utterance
            so a single voice is sampled for the whole reference).
            ``run_in_modal`` (Phase 10.4) renders the design on Modal instead
            of the local GPU worker; it needs ``MODAL_ENABLED`` plus
            credentials and is refused non-retryably otherwise.

    Returns:
        Dict with ``name``, ``sha256``, ``sample_rate``, ``duration_seconds``,
        ``design_model`` and ``design_seconds``. The audio is reachable by
        name through the staging endpoint, not in the result.

    Raises:
        ApplicationError: When the payload is invalid, the model fails or the
            audio cannot be staged. Staging failures that a retry could fix
            (network, 5xx) stay retryable; a missing key does not. A
            ``run_in_modal`` run without Modal enabled or configured is
            refused non-retryably.
    """
    name = str(payload.get("name") or "")
    instruct = str(payload.get("instruct") or "")
    ref_text = str(payload.get("ref_text") or "")
    language = str(payload.get("language") or "en")
    segments = [str(part) for part in (payload.get("ref_segments") or [])]
    if not segments and ref_text:
        segments = [ref_text]
    if not name or not instruct or not segments:
        raise ApplicationError(
            "design_voice_activity needs name, instruct and ref_text",
            non_retryable=True,
        )

    run_in_modal = bool(payload.get("run_in_modal"))

    activity.logger.info(
        f"Designing voice name={name} language={language} segments={len(segments)} "
        f"run_in_modal={run_in_modal}"
    )
    started = time.monotonic()
    seed = zlib.crc32(f"qwen-voice-design:{name}".encode())
    try:
        if run_in_modal:
            # The switch is per-run, so misconfiguration must fail the run
            # loudly instead of silently designing on the local worker.
            if not settings.modal_enabled:
                raise ApplicationError(
                    "voice design requires modal, but modal is disabled",
                    non_retryable=True,
                )
            if not settings.modal_token_id or not settings.modal_token_secret:
                raise ApplicationError(
                    "modal credentials are not configured",
                    non_retryable=True,
                )
            from src.core.modal_client import design_voice as design_voice_modal

            wav, sample_rate = await design_voice_modal(
                segments=segments,
                instruct=instruct,
                language=language,
                seed=seed,
                temperature=settings.qwen_tts_design_temperature,
                top_p=settings.qwen_tts_design_top_p,
            )
        else:
            from src.core.local_tts import design_voice

            # The synthesis is a blocking GPU call; keep the event loop free so
            # the worker can still heartbeat while the model generates.
            wav, sample_rate = await asyncio.to_thread(
                design_voice, segments, instruct, language, seed
            )
    except ApplicationError:
        raise
    except Exception as exc:
        raise ApplicationError(f"voice design failed for {name}: {exc}") from exc
    elapsed = time.monotonic() - started

    # Ship the bytes before returning: the result must stay small enough for
    # the workflow history to survive a whole 24-voice batch.
    from src.api.services.voice_staging import VoiceStagingError, stage_from_design

    try:
        staged = await stage_from_design(name, wav)
    except VoiceStagingError as exc:
        raise ApplicationError(
            f"voice staging failed for {name}: {exc}",
            non_retryable=not exc.retryable,
        ) from exc

    activity.logger.info(
        f"Voice designed name={name} design_seconds={round(elapsed, 2)} "
        f"sha256={sha256_bytes(wav)} sample_rate={sample_rate} staged_bytes={staged}"
    )
    return {
        "name": name,
        "sha256": sha256_bytes(wav),
        "sample_rate": sample_rate,
        "duration_seconds": wav_duration(wav),
        "design_model": settings.qwen_tts_design_model,
        "design_seconds": round(elapsed, 2),
    }


@activity.defn
async def store_voice_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Ship a designed reference to the gateway, which stores it with its manifest.

    The design worker only stages bytes, so this is the step that makes the
    reference real — on the gateway side now, since P6 owns ``refs/`` and the
    manifest. The audio is read back from the staging endpoint the design step
    wrote to, never from the workflow payload, and the entry that comes back
    describes the bytes the gateway measured.

    Args:
        payload: The profile fields (``name``, ``instruct``, ``ref_text``)
            plus the design result (``sha256`` and optionally
            ``design_model``).

    Returns:
        Dict with ``name``, ``file``, ``sha256``, ``sample_rate`` and
        ``duration_seconds``.

    Raises:
        ApplicationError: When the payload is invalid, nothing was staged, the
            audio cannot be read, or it does not match the digest the design
            step reported. All of them are non-retryable: a retry cannot fix
            the bytes. A gateway refusal keeps the gateway's own message and
            is non-retryable only when it said so (404/422), so an outage
            retries and a wrong profile does not.
    """
    name = str(payload.get("name") or "")
    instruct = str(payload.get("instruct") or "")
    ref_text = str(payload.get("ref_text") or "")
    digest = str(payload.get("sha256") or "")
    if not name or not instruct or not ref_text or not digest:
        raise ApplicationError(
            "store_voice_activity needs name, instruct, ref_text and sha256",
            non_retryable=True,
        )

    from src.api.services.voice_staging import VoiceStagingError, clear_staged, read_staged

    try:
        wav = read_staged(name)
    except VoiceStagingError as exc:
        raise ApplicationError(
            f"voice audio for {name} is unavailable: {exc}",
            non_retryable=not exc.retryable,
        ) from exc
    if sha256_bytes(wav) != digest:
        raise ApplicationError(
            f"voice audio for {name} does not match the reported digest",
            non_retryable=True,
        )
    try:
        wav_format(wav)
        wav_duration(wav)
    except AudioError as exc:
        raise ApplicationError(
            f"voice audio for {name} is not a valid WAV: {exc}", non_retryable=True
        ) from exc
    if wav_duration(wav) <= 0.0:
        raise ApplicationError(f"voice audio for {name} is empty", non_retryable=True)

    try:
        entry = await store_reference(
            name,
            wav,
            instruct=instruct,
            ref_text=ref_text,
            design_model=str(payload.get("design_model") or settings.qwen_tts_design_model),
        )
    except VoiceCatalogRequestError as exc:
        raise ApplicationError(
            f"voice reference for {name} could not be stored: {exc}",
            non_retryable=not exc.retryable,
        ) from exc

    clear_staged(name)

    activity.logger.info(
        f"Voice stored name={name} duration_seconds={entry.duration_seconds} sha256={entry.sha256}"
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
        payload: Dict with ``name`` and ``ref_text``.

    Returns:
        Dict with ``name`` and ``uploaded``.

    Raises:
        ApplicationError: When the voice is missing or the upload fails.
    """
    name = str(payload.get("name") or "")
    if not name:
        raise ApplicationError("upload_voice_activity needs a name", non_retryable=True)
    ref_text = str(payload.get("ref_text") or "")

    from src.core.modal_client import upload_voice

    try:
        wav = await fetch_reference_audio(name)
    except VoiceCatalogRequestError as exc:
        raise ApplicationError(
            f"voice reference for {name} could not be read: {exc}",
            non_retryable=not exc.retryable,
        ) from exc
    if wav is None:
        raise ApplicationError(f"voice '{name}' has no reference audio", non_retryable=True)
    try:
        await upload_voice(name, wav, ref_text)
    except Exception as exc:
        raise ApplicationError(f"voice upload failed for {name}: {exc}") from exc
    activity.logger.info(f"Voice uploaded name={name}")
    return {"name": name, "uploaded": True}


@activity.defn
async def voice_catalog_status_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Report which catalog voices are generated, missing or stale.

    Reads the catalog snapshot so the workflow stays deterministic: a workflow
    must not do I/O. The snapshot is refreshed first, because this activity is
    what a design run polls to know what is left to do.

    Args:
        payload: Dict with ``names`` (optional subset of profile names).

    Returns:
        Dict with ``generated``, ``missing``, ``stale``, ``pending`` and
        ``profiles`` (the full profile dicts for pending names).

    Raises:
        VoiceCatalogRequestError: When the gateway cannot be reached.
    """
    names = [str(name) for name in (payload.get("names") or [])]
    snapshot = await fetch_catalog(max_age=0)
    profiles = snapshot.profiles
    by_name = {profile.name: profile for profile in profiles}
    targets = [by_name[name] for name in names if name in by_name] if names else profiles
    manifest = snapshot.manifest
    pending = set(missing_or_stale(targets, manifest))
    return {
        "generated": sorted(p.name for p in targets if p.name not in pending and p.enabled),
        "missing": sorted(pending),
        "stale": sorted(pending),
        "pending": sorted(pending),
        "profiles": [p.model_dump(mode="json") for p in targets if p.name in pending],
    }
