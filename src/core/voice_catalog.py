"""Voice catalog: load profiles, resolve references, detect drift.

The catalog is the source of truth for which voices exist and how they were
produced. Profiles live in a versionable JSON file; the generated references
and their manifest live under the runtime voices directory and are never
committed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from src.api.models.voices import VoiceManifestEntry, VoiceProfile

DEFAULT_PROFILES_FILE = "voice_profiles.json"
DEFAULT_VOICES_DIR = "data/voices"
MANIFEST_NAME = "manifest.json"
REFS_DIR = "refs"


class VoiceCatalogError(RuntimeError):
    """Base error for the voice catalog."""


def sha256_text(text: str) -> str:
    """Return the SHA-256 digest of a text string.

    Args:
        text: Text to digest.

    Returns:
        Hex SHA-256 digest.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_profiles(path: str | Path | None = None) -> list[VoiceProfile]:
    """Load and validate the voice catalog.

    Args:
        path: Catalog JSON path. Defaults to ``voice_profiles.json``.

    Returns:
        The validated profiles, in file order.

    Raises:
        VoiceCatalogError: If the file is missing, unreadable, not valid JSON,
            or contains duplicate names.
    """
    catalog = Path(path) if path else Path(DEFAULT_PROFILES_FILE)
    if not catalog.is_file():
        raise VoiceCatalogError(f"voice catalog not found: {catalog}")
    try:
        raw = json.loads(catalog.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VoiceCatalogError(f"voice catalog is not readable JSON: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), list):
        raise VoiceCatalogError("voice catalog needs a 'profiles' list")

    profiles = [VoiceProfile.model_validate(item) for item in raw["profiles"]]
    names = [profile.name for profile in profiles]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise VoiceCatalogError(f"duplicate voice profile names: {duplicates}")
    return profiles


def load_manifest(path: str | Path | None = None) -> dict[str, VoiceManifestEntry]:
    """Load the generated-voices manifest.

    A missing manifest is not an error: it simply means no voice has been
    generated yet.

    Args:
        path: Manifest JSON path. Defaults to ``data/voices/manifest.json``.

    Returns:
        Mapping of profile name to its manifest entry.

    Raises:
        VoiceCatalogError: If the manifest exists but is not valid JSON.
    """
    manifest_path = Path(path) if path else Path(DEFAULT_VOICES_DIR) / MANIFEST_NAME
    if not manifest_path.is_file():
        return {}
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VoiceCatalogError(f"voice manifest is not readable JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise VoiceCatalogError("voice manifest must be a JSON object")
    return {name: VoiceManifestEntry.model_validate(entry) for name, entry in raw.items()}


def is_stale(profile: VoiceProfile, entry: VoiceManifestEntry) -> bool:
    """Report whether a profile changed after its reference was generated.

    Args:
        profile: The current catalog profile.
        entry: The manifest entry for that profile.

    Returns:
        True when the instruct or ref_text no longer match the reference.
    """
    return entry.instruct_sha256 != sha256_text(
        profile.instruct
    ) or entry.ref_text_sha256 != sha256_text(profile.ref_text)


def missing_or_stale(
    profiles: list[VoiceProfile],
    manifest: dict[str, VoiceManifestEntry],
) -> list[str]:
    """List enabled profiles that need (re)generation.

    A profile is listed when it has no manifest entry at all, or when its
    profile text drifted from the reference that was generated.

    Args:
        profiles: Catalog profiles.
        manifest: Generated-voices manifest.

    Returns:
        Sorted profile names that the design flow should (re)generate.
    """
    pending: list[str] = []
    for profile in profiles:
        if not profile.enabled:
            continue
        entry = manifest.get(profile.name)
        if entry is None or is_stale(profile, entry):
            pending.append(profile.name)
    return sorted(pending)


def resolve_reference(
    profile: VoiceProfile,
    manifest: dict[str, VoiceManifestEntry],
    voices_dir: str | Path | None = None,
) -> Path | None:
    """Return the reference WAV path for a profile.

    Args:
        profile: Catalog profile to resolve.
        manifest: Generated-voices manifest.
        voices_dir: Root of the runtime voices directory.

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


def write_manifest(
    manifest: dict[str, VoiceManifestEntry],
    path: str | Path | None = None,
) -> None:
    """Persist the generated-voices manifest.

    Args:
        manifest: Mapping of profile name to manifest entry.
        path: Manifest JSON path. Defaults to ``data/voices/manifest.json``.
    """
    manifest_path = Path(path) if path else Path(DEFAULT_VOICES_DIR) / MANIFEST_NAME
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {name: entry.model_dump(mode="json") for name, entry in manifest.items()}
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
