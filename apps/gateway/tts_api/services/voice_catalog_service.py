"""Voice catalog ownership: profiles, manifest and reference audio (P6).

The gateway is the single owner of the catalog, which used to live in
news-engine's ``voice_profiles.json`` plus its ``data/voices`` directory:

* ``VOICE_PROFILES_FILE`` — the committed catalog, versioned with the code.
* ``VOICE_CATALOG_DIR`` — the runtime half: ``manifest.json``, ``refs/*.wav``
  and the operator-created ``custom_profiles.json``.

news-engine reads all of it over ``/api/v1/voices/catalog`` and never touches a
catalog file of its own, so one deployment has exactly one truth about which
voices exist and which references are ready to clone.
"""

from __future__ import annotations

import io
import json
import threading
import wave
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from tts_contract import (
    STATUS_GENERATED,
    STATUS_PENDING,
    STATUS_STALE,
    VoiceCatalogError,
    VoiceCatalogResponse,
    VoiceManifestEntry,
    VoiceProfile,
    classify_status,
    is_voice_slug,
    load_catalog_profiles,
    missing_or_stale,
    sha256_bytes,
    sha256_text,
)

from tts_api.settings import get_settings

#: Runtime file names under ``VOICE_CATALOG_DIR``.
MANIFEST_NAME = "manifest.json"
CUSTOM_PROFILES_NAME = "custom_profiles.json"
REFS_DIR_NAME = "refs"

#: Statuses of a catalog voice's reference audio, re-exported for the router.
__all__ = [
    "STATUS_GENERATED",
    "STATUS_PENDING",
    "STATUS_STALE",
    "VoiceCatalogConflictError",
    "VoiceCatalogNotFoundError",
    "VoiceCatalogService",
    "VoiceCatalogValidationError",
]


class VoiceCatalogConflictError(VoiceCatalogError):
    """A profile name is already taken (409)."""


class VoiceCatalogNotFoundError(VoiceCatalogError):
    """The requested profile or reference does not exist (404)."""


class VoiceCatalogValidationError(VoiceCatalogError):
    """The payload or the audio is not usable (422)."""


def _read_wav_info(data: bytes) -> tuple[int, float]:
    """Return ``(sample_rate, duration_seconds)`` of a WAV payload.

    Args:
        data: Raw WAV bytes.

    Returns:
        Tuple of sample rate in Hz and duration in seconds.

    Raises:
        VoiceCatalogValidationError: When the bytes are not a readable WAV
            with at least one frame.
    """
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            sample_rate = reader.getframerate()
            frames = reader.getnframes()
    except (wave.Error, EOFError) as exc:
        raise VoiceCatalogValidationError(f"reference audio is not a readable WAV: {exc}") from exc
    if sample_rate <= 0 or frames <= 0:
        raise VoiceCatalogValidationError("reference audio has no frames")
    return sample_rate, frames / sample_rate


class VoiceCatalogService:
    """Read and write the catalog the gateway owns.

    The service is synchronous on purpose: every operation touches a handful of
    small local files, and the endpoints that call it stay readable instead of
    spreading ``await`` through file handling. Writes are serialised with a
    lock so two concurrent design runs cannot lose a manifest update.
    """

    def __init__(
        self,
        *,
        profiles_file: str | Path | None = None,
        catalog_dir: str | Path | None = None,
    ) -> None:
        """Open the catalog.

        Args:
            profiles_file: Committed catalog path. Defaults to
                ``settings.VOICE_PROFILES_FILE``.
            catalog_dir: Runtime catalog directory. Defaults to
                ``settings.VOICE_CATALOG_DIR``.
        """
        settings = get_settings()
        self._profiles_file = Path(profiles_file) if profiles_file else settings.VOICE_PROFILES_FILE
        self._catalog_dir = Path(catalog_dir) if catalog_dir else settings.VOICE_CATALOG_DIR
        self._settings = settings
        self._lock = threading.Lock()

    # --- paths ---------------------------------------------------------------

    @property
    def profiles_file(self) -> Path:
        """Path of the committed catalog file."""
        return self._profiles_file

    @property
    def catalog_dir(self) -> Path:
        """Path of the runtime catalog directory."""
        return self._catalog_dir

    @property
    def manifest_path(self) -> Path:
        """Path of the generated-voice manifest."""
        return self._catalog_dir / MANIFEST_NAME

    @property
    def custom_profiles_path(self) -> Path:
        """Path of the operator-created profiles file."""
        return self._catalog_dir / CUSTOM_PROFILES_NAME

    @property
    def refs_dir(self) -> Path:
        """Path of the directory holding the reference WAVs."""
        return self._catalog_dir / REFS_DIR_NAME

    # --- reads ---------------------------------------------------------------

    def profiles(self) -> list[VoiceProfile]:
        """Return the committed profiles in file order.

        Returns:
            The validated committed profiles.

        Raises:
            VoiceCatalogError: When the committed catalog cannot be read.
        """
        return load_catalog_profiles(self._profiles_file)

    def custom_profiles(self) -> list[VoiceProfile]:
        """Return the operator-created profiles, empty when none exist yet.

        Returns:
            The validated custom profiles in file order.

        Raises:
            VoiceCatalogError: When the file exists but is not readable JSON.
        """
        return self._load_custom_profiles()

    def all_profiles(self) -> list[VoiceProfile]:
        """Return committed plus custom profiles, rejecting name clashes.

        Returns:
            The committed profiles in file order, then the custom ones.

        Raises:
            VoiceCatalogError: When a name is defined in both files.
        """
        profiles = self.profiles()
        custom = self._load_custom_profiles()
        known = {profile.name for profile in profiles}
        clashes = sorted(known & {profile.name for profile in custom})
        if clashes:
            raise VoiceCatalogError(f"duplicate voice profile names: {clashes}")
        return [*profiles, *custom]

    def manifest(self) -> dict[str, VoiceManifestEntry]:
        """Return the generated-voice manifest.

        A missing manifest simply means nothing has been generated yet.

        Returns:
            Mapping of profile name to its manifest entry.

        Raises:
            VoiceCatalogError: When the manifest exists but is not valid JSON.
        """
        if not self.manifest_path.is_file():
            return {}
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VoiceCatalogError(f"voice manifest is not readable JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise VoiceCatalogError("voice manifest must be a JSON object")
        return {name: VoiceManifestEntry.model_validate(entry) for name, entry in raw.items()}

    def catalog(self) -> VoiceCatalogResponse:
        """Build the payload of ``GET /api/v1/voices/catalog``.

        Returns:
            Profiles, manifest, per-profile status and the generated/missing
            name lists a client needs to render the admin GUI.

        Raises:
            VoiceCatalogError: When the committed catalog, the custom profiles
                or the manifest cannot be read.
        """
        committed = self.profiles()
        custom = self._load_custom_profiles()
        known = {profile.name for profile in committed}
        clashes = sorted(known & {profile.name for profile in custom})
        if clashes:
            raise VoiceCatalogError(f"duplicate voice profile names: {clashes}")
        profiles = [*committed, *custom]
        manifest = self.manifest()
        pending = set(missing_or_stale(profiles, manifest))
        statuses = {
            profile.name: classify_status(
                profile,
                manifest,
                has_audio=self.reference_path(profile.name) is not None,
            )
            for profile in profiles
        }
        return VoiceCatalogResponse(
            profiles=profiles,
            generated=sorted(
                profile.name
                for profile in profiles
                if profile.name not in pending and profile.enabled
            ),
            missing=sorted(pending),
            stale=sorted(pending),
            custom=sorted(profile.name for profile in custom),
            statuses=statuses,
            manifest=manifest,
        )

    def reference_path(self, name: str) -> Path | None:
        """Return the reference WAV of a profile, when it exists on disk.

        Args:
            name: Profile name, optionally with a ``.wav`` suffix.

        Returns:
            Path of the reference, or None when the name is unsafe, the profile
            is unknown, the manifest entry escapes the catalog directory or the
            file does not exist.
        """
        stem = name[:-4] if name.lower().endswith(".wav") else name
        if not is_voice_slug(stem):
            return None
        entry = self.manifest().get(stem)
        relative = entry.file if entry is not None else f"{REFS_DIR_NAME}/{stem}.wav"
        root = self._catalog_dir.resolve()
        wav = (root / relative).resolve()
        if not wav.is_relative_to(root) or not wav.is_file():
            return None
        return wav

    def read_reference(self, name: str) -> bytes:
        """Return the reference WAV bytes of a profile.

        Args:
            name: Profile name, optionally with a ``.wav`` suffix.

        Returns:
            The WAV bytes.

        Raises:
            VoiceCatalogNotFoundError: When the name is unsafe, unknown or has
                no reference on disk.
        """
        wav = self.reference_path(name)
        if wav is None:
            raise VoiceCatalogNotFoundError(f"voice reference not found: {name!r}")
        try:
            return wav.read_bytes()
        except OSError as exc:
            raise VoiceCatalogNotFoundError(f"voice reference unreadable: {name!r}") from exc

    # --- writes --------------------------------------------------------------

    def store_reference(
        self,
        name: str,
        wav: bytes,
        *,
        instruct: str,
        ref_text: str,
        design_model: str = "",
    ) -> VoiceManifestEntry:
        """Persist a designed reference and its manifest entry.

        The gateway measures and digests the audio itself instead of trusting
        what the caller reports, so the manifest can never disagree with the
        bytes it describes.

        Args:
            name: Profile name (a slug, without the ``.wav`` suffix).
            wav: Raw WAV bytes of the reference.
            instruct: Profile instruct at generation time, for the drift guard.
            ref_text: Profile transcript at generation time, for the drift guard.
            design_model: Model that produced the reference.

        Returns:
            The stored manifest entry.

        Raises:
            VoiceCatalogValidationError: When the name, the audio or its
                duration is not acceptable.
            VoiceCatalogNotFoundError: When no catalog profile has that name.
            VoiceCatalogError: When the manifest cannot be read back.
        """
        if not is_voice_slug(name):
            raise VoiceCatalogValidationError(f"invalid voice name: {name!r}")
        if not wav:
            raise VoiceCatalogValidationError(f"empty reference audio for {name!r}")
        max_bytes = self._settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
        if len(wav) > max_bytes:
            raise VoiceCatalogValidationError(
                f"reference audio for {name!r} exceeds {max_bytes} bytes"
            )
        sample_rate, duration = _read_wav_info(wav)
        limit = self._settings.MAX_REFERENCE_DURATION_SEC
        if limit and duration > limit:
            raise VoiceCatalogValidationError(
                f"reference audio for {name!r} lasts {duration:.1f}s (limit {limit:.0f}s)"
            )
        if not instruct.strip() or not ref_text.strip():
            raise VoiceCatalogValidationError(
                f"reference audio for {name!r} needs instruct and ref_text"
            )
        if not any(profile.name == name for profile in self.all_profiles()):
            raise VoiceCatalogNotFoundError(f"unknown voice profile: {name!r}")

        entry = VoiceManifestEntry(
            file=f"{REFS_DIR_NAME}/{name}.wav",
            sha256=sha256_bytes(wav),
            sample_rate=sample_rate,
            duration_seconds=duration,
            design_model=design_model,
            instruct_sha256=sha256_text(instruct),
            ref_text_sha256=sha256_text(ref_text),
            created_at=datetime.now(UTC).isoformat(),
        )
        with self._lock:
            self.refs_dir.mkdir(parents=True, exist_ok=True)
            target = self.refs_dir / f"{name}.wav"
            tmp = target.with_name(f"{target.name}.tmp")
            tmp.write_bytes(wav)
            tmp.replace(target)
            manifest = self.manifest()
            manifest[name] = entry
            self._write_manifest(manifest)
        logger.info(
            "voice_reference_stored name={} duration_seconds={} sha256={}",
            name,
            round(entry.duration_seconds, 3),
            entry.sha256,
        )
        return entry

    def save_custom(self, profile: VoiceProfile) -> VoiceProfile:
        """Append an operator-created profile to the custom catalog.

        Args:
            profile: Validated profile to append.

        Returns:
            The saved profile.

        Raises:
            VoiceCatalogConflictError: When the name is already taken by a
                custom profile or by the committed catalog.
            VoiceCatalogError: When the existing custom file cannot be read
                (it is never overwritten blindly).
        """
        with self._lock:
            existing = self._load_custom_profiles()
            if any(saved.name == profile.name for saved in existing):
                raise VoiceCatalogConflictError(
                    f"custom voice profile already exists: {profile.name!r}"
                )
            if any(saved.name == profile.name for saved in self.profiles()):
                raise VoiceCatalogConflictError(
                    f"voice profile name already in the catalog: {profile.name!r}"
                )
            payload = {
                "profiles": [saved.model_dump(mode="json") for saved in [*existing, profile]]
            }
            self.custom_profiles_path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(
                self.custom_profiles_path,
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            )
        logger.info("voice_profile_custom_saved name={}", profile.name)
        return profile

    # --- internals -----------------------------------------------------------

    def _load_custom_profiles(self) -> list[VoiceProfile]:
        """Read the custom profiles file, which may not exist yet.

        Returns:
            The validated custom profiles in file order.

        Raises:
            VoiceCatalogError: When the file exists but is unusable.
        """
        path = self.custom_profiles_path
        if not path.is_file():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VoiceCatalogError(f"custom voice catalog is not readable JSON: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), list):
            raise VoiceCatalogError("custom voice catalog needs a 'profiles' list")

        profiles: list[VoiceProfile] = []
        for item in raw["profiles"]:
            if not isinstance(item, dict):
                raise VoiceCatalogError("custom voice catalog profiles must be objects")
            try:
                profiles.append(VoiceProfile.model_validate(item))
            except ValueError as exc:
                name = item.get("name", "?")
                raise VoiceCatalogError(f"custom voice profile {name!r} is invalid: {exc}") from exc

        names = [profile.name for profile in profiles]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise VoiceCatalogError(f"duplicate custom voice profile names: {duplicates}")
        return profiles

    def _write_manifest(self, manifest: dict[str, VoiceManifestEntry]) -> None:
        """Persist the manifest atomically.

        Args:
            manifest: Mapping of profile name to its manifest entry.
        """
        payload = {name: entry.model_dump(mode="json") for name, entry in manifest.items()}
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(
            self.manifest_path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        """Write a text file so a crash never leaves a half-written one.

        Args:
            path: Target file.
            text: Full contents to write.
        """
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
