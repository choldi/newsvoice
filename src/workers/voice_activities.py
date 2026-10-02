"""Temporal activities for the voice catalog (Phase 7.8/7.9).

The activities are split across two machines on purpose:

* ``design_voice_activity`` runs on the voice-design queue, polled by the GPU
  box. It loads Qwen3-TTS, synthesises the reference and returns the audio
  gzipped so it can travel over the wire. It never touches the server disk.
* ``store_voice_activity``, ``upload_voice_activity`` and
  ``voice_catalog_status_activity`` run on the narration queue, which owns
  ``data/voices``.

Everything with side effects lives here; the workflow only orchestrates.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from src.api.models.voices import VoiceManifestEntry
from src.core.audio import AudioError, wav_duration, wav_format
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


def _gzip_b64(wav: bytes) -> str:
    """Compress and encode audio so it fits in an activity result.

    Args:
        wav: Raw WAV bytes.

    Returns:
        Base64 of the gzip stream, as ASCII text.
    """
    return base64.b64encode(gzip.compress(wav)).decode("ascii")


def _gunzip_b64(encoded: str) -> bytes:
    """Decode an audio payload produced by :func:`_gzip_b64`.

    Args:
        encoded: Base64 of a gzip stream.

    Returns:
        The original WAV bytes.

    Raises:
        ValueError: If the payload is not valid base64 or gzip.
    """
    return gzip.decompress(base64.b64decode(encoded))


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
    """Synthesise a reference voice on the GPU box and return it for shipping.

    The result carries the audio itself, gzipped: the design worker has no
    access to ``data/voices``, so writing the file is a separate activity.

    Args:
        payload: Dict with ``name``, ``instruct``, ``ref_text`` and
            ``language``. Optional ``ref_segments`` replaces ``ref_text`` as
            the list of segments to speak (they are concatenated with a short
            silence, which keeps a long reference inside one generation).

    Returns:
        Dict with ``name``, ``wav_gz_b64``, ``sha256``, ``sample_rate``,
        ``duration_seconds``, ``design_model`` and ``design_seconds``.

    Raises:
        ApplicationError: When the payload is invalid or the model fails.
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

    from src.core.local_tts import design_voice

    activity.logger.info("Designing voice", name=name, language=language, segments=len(segments))
    started = time.monotonic()
    try:
        # The synthesis is a blocking GPU call; keep the event loop free so the
        # worker can still heartbeat while the model generates.
        wav, sample_rate = await asyncio.to_thread(design_voice, segments, instruct, language)
    except Exception as exc:
        raise ApplicationError(f"voice design failed for {name}: {exc}") from exc
    elapsed = time.monotonic() - started

    activity.logger.info(
        "Voice designed",
        name=name,
        design_seconds=round(elapsed, 2),
        sha256=sha256_bytes(wav),
        sample_rate=sample_rate,
    )
    return {
        "name": name,
        "wav_gz_b64": _gzip_b64(wav),
        "sha256": sha256_bytes(wav),
        "sample_rate": sample_rate,
        "duration_seconds": wav_duration(wav),
        "design_model": settings.qwen_tts_design_model,
        "design_seconds": round(elapsed, 2),
    }


@activity.defn
async def store_voice_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Write a designed reference to the voices directory and the manifest.

    Runs on the server, which owns ``data/voices``: the design worker never
    sees that path, so this is the step that makes the reference real.

    Args:
        payload: The profile fields (``name``, ``instruct``, ``ref_text``)
            plus the design result (``wav_gz_b64``, ``sha256`` and optionally
            ``design_model``).

    Returns:
        Dict with ``name``, ``file``, ``sha256``, ``sample_rate`` and
        ``duration_seconds``.

    Raises:
        ApplicationError: When the payload is invalid, the audio cannot be
            read, or it does not match the digest the design step reported.
            All of them are non-retryable: a retry cannot fix the bytes.
    """
    name = str(payload.get("name") or "")
    instruct = str(payload.get("instruct") or "")
    ref_text = str(payload.get("ref_text") or "")
    encoded = str(payload.get("wav_gz_b64") or "")
    digest = str(payload.get("sha256") or "")
    if not name or not instruct or not ref_text or not encoded:
        raise ApplicationError(
            "store_voice_activity needs name, instruct, ref_text and wav_gz_b64",
            non_retryable=True,
        )

    try:
        wav = _gunzip_b64(encoded)
    except Exception as exc:
        raise ApplicationError(
            f"voice audio for {name} is unreadable: {exc}", non_retryable=True
        ) from exc
    if sha256_bytes(wav) != digest:
        raise ApplicationError(
            f"voice audio for {name} does not match the reported digest",
            non_retryable=True,
        )
    try:
        fmt = wav_format(wav)
        duration = wav_duration(wav)
    except AudioError as exc:
        raise ApplicationError(
            f"voice audio for {name} is not a valid WAV: {exc}", non_retryable=True
        ) from exc
    if duration <= 0.0:
        raise ApplicationError(f"voice audio for {name} is empty", non_retryable=True)

    voices_dir = Path(settings.voices_dir)
    refs_dir = voices_dir / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    ref_path = refs_dir / f"{name}.wav"
    ref_path.write_bytes(wav)

    entry = VoiceManifestEntry(
        file=f"refs/{name}.wav",
        sha256=digest,
        sample_rate=fmt.framerate,
        duration_seconds=duration,
        design_model=str(payload.get("design_model") or settings.qwen_tts_design_model),
        instruct_sha256=sha256_text(instruct),
        ref_text_sha256=sha256_text(ref_text),
        created_at=datetime.now(UTC).isoformat(),
    )
    manifest_path = voices_dir / "manifest.json"
    manifest = load_manifest(manifest_path)
    manifest[name] = entry
    write_manifest(manifest, manifest_path)

    activity.logger.info(
        "Voice stored",
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
