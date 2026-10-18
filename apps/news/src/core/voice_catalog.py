"""Voice catalog: the models, the drift rules and the snapshot readers.

Since P6 the gateway owns the catalog: the committed ``voice_profiles.json``,
the generated manifest, the reference WAVs and the operator-created profiles
all live next to the TTS API, which serves them from
``GET /api/v1/voices/catalog``. news-engine keeps no catalog file of its own.

This module is the shared vocabulary both sides agree on, plus the readers the
rest of the app calls:

- the models, the error type and the staleness rules are re-exported from
  :mod:`tts_contract`, the package neither app may disagree with;
- :func:`load_profiles`, :func:`load_manifest` and :func:`load_custom_profiles`
  with no arguments serve the snapshot
  :mod:`src.api.services.voice_catalog_client` loaded from the gateway (it
  blocks only to prime a cold process, once);
- the same functions with an explicit ``path`` still read a file of the shared
  JSON format, which keeps one-off inspections and the drift tests pinned to
  what is actually on disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tts_contract import (
    VoiceCatalogError,
    VoiceManifestEntry,
    VoiceProfile,
    VoiceSource,
    is_stale,
    is_voice_slug,
    load_catalog_profiles,
    missing_or_stale,
    sha256_bytes,
    sha256_text,
)

from src.api.services.voice_catalog_client import (
    fetch_catalog_blocking,
)

__all__ = [
    "DEFAULT_VOICES_DIR",
    "VoiceCatalogError",
    "VoiceManifestEntry",
    "VoiceProfile",
    "VoiceSource",
    "is_stale",
    "is_voice_slug",
    "load_custom_profiles",
    "load_manifest",
    "load_profiles",
    "missing_or_stale",
    "profile_dict",
    "ref_text_for",
    "resolve_reference",
    "sha256_bytes",
    "sha256_text",
    "write_manifest",
]

#: Where a reference would live if it were local. The catalog moved to the
#: gateway in P6; the name survives for :func:`resolve_reference`, which
#: computes a path from a manifest entry instead of touching the network.
DEFAULT_VOICES_DIR = "data/voices"


def load_profiles(path: str | Path | None = None) -> list[VoiceProfile]:
    """Load and validate the voice catalog.

    The catalog may declare reference segments once per language under a
    ``languages`` block, which every profile of that language then inherits:
    all voices of a language are cloned from the same transcript, so it only
    makes sense to write it once. A profile may still spell out its own text,
    but it must then agree with the block, so the two can never drift apart.

    With no explicit ``path`` this returns the snapshot served by the gateway,
    already merged with the operator-created profiles and already checked for
    name clashes. An explicit ``path`` reads only that file, which keeps tests
    and one-off inspections pinned to what is on disk.

    Args:
        path: Catalog JSON path. Defaults to the gateway's catalog snapshot.

    Returns:
        The validated profiles: committed ones in file order, then the custom
        ones.

    Raises:
        VoiceCatalogError: If the file is missing, unreadable, not valid JSON,
            contains duplicate names, or leaves a profile without any
            reference text — or if the gateway that owns the snapshot could
            not be reached.
    """
    if path is not None:
        return load_catalog_profiles(Path(path))
    return list(fetch_catalog_blocking().profiles)


def load_custom_profiles() -> list[VoiceProfile]:
    """Load the operator-created profiles (the gateway's custom catalog).

    Returns:
        The validated custom profiles in file order; empty when the operator
        has created none.

    Raises:
        VoiceCatalogError: If the gateway could not be reached or answered
            something that is not a catalog.
    """
    snapshot = fetch_catalog_blocking()
    custom = set(snapshot.custom)
    return [profile for profile in snapshot.profiles if profile.name in custom]


def load_manifest(path: str | Path | None = None) -> dict[str, VoiceManifestEntry]:
    """Load the generated-voices manifest.

    A missing manifest is not an error: it simply means no voice has been
    generated yet.

    Args:
        path: Manifest JSON path. With no path the gateway's manifest is
            served from the catalog snapshot.

    Returns:
        Mapping of profile name to its manifest entry.

    Raises:
        VoiceCatalogError: If the manifest exists but is not valid JSON, or
            the gateway that owns it could not be reached.
    """
    if path is None:
        return dict(fetch_catalog_blocking().manifest)
    manifest_path = Path(path)
    if not manifest_path.is_file():
        return {}
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VoiceCatalogError(f"voice manifest is not readable JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise VoiceCatalogError("voice manifest must be a JSON object")
    return {name: VoiceManifestEntry.model_validate(entry) for name, entry in raw.items()}


def write_manifest(manifest: dict[str, VoiceManifestEntry], path: str | Path) -> None:
    """Persist a manifest to a JSON file of the format the gateway serves.

    The gateway writes its own manifest as it stores references; this is the
    plain file writer of that same format, used by exports and by the tests
    that pin a manifest on disk.

    Args:
        manifest: Mapping of profile name to manifest entry.
        path: Manifest JSON path.
    """
    manifest_path = Path(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {name: entry.model_dump(mode="json") for name, entry in manifest.items()}
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def resolve_reference(
    profile: VoiceProfile,
    manifest: dict[str, VoiceManifestEntry],
    voices_dir: str | Path | None = None,
) -> Path | None:
    """Return the reference WAV path for a profile inside a voices directory.

    Args:
        profile: Catalog profile to resolve.
        manifest: Generated-voices manifest.
        voices_dir: Root of the voices directory.

    Returns:
        The reference path, or None when the voice has not been generated.

    Raises:
        VoiceCatalogError: If the manifest entry is stale, because cloning
            with a mismatched transcript degrades quality silently.
    """
    base = Path(voices_dir) if voices_dir else Path(DEFAULT_VOICES_DIR)
    entry = manifest.get(profile.name)
    if entry is None:
        return None
    if is_stale(profile, entry):
        raise VoiceCatalogError(
            f"voice '{profile.name}' is stale: regenerate the reference before cloning"
        )
    return base / entry.file


def ref_text_for(profile: VoiceProfile) -> str:
    """Return the transcript that must accompany a reference sample.

    Qwen3-TTS clones in in-context-learning mode, which requires the exact
    text that produced the reference. ZONOS2 does not need it, but the field is
    mandatory either way so a recording can be used as a source too.

    Args:
        profile: Catalog profile.

    Returns:
        The reference transcript.
    """
    return profile.ref_text


def profile_dict(profile: VoiceProfile) -> dict[str, Any]:
    """Return a JSON-serializable dump of a profile.

    Args:
        profile: Catalog profile.

    Returns:
        The profile as a plain dict.
    """
    return profile.model_dump(mode="json")
