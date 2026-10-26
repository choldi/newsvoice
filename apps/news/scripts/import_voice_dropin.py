"""Import operator voices from a drop-in directory into the gateway catalog.

Every voice lives in its own subdirectory of the drop-in root, with a
``voice.json`` describing it plus the reference audio and its exact transcript::

    voice_dropin/
      _example/
        voice.json            # template only; ignored by the import
      tech_anchor_m/
        voice.json
        sample.wav
        sample.txt
      tech_expert_f/
        voice.json
        sample.wav
        sample.txt

``voice.json`` fields:

* ``name``       stable slug (``^[a-z0-9][a-z0-9_-]*$``); the catalog profile name
* ``label``      human label for the admin GUI (defaults to ``name``)
* ``role``       role hint: anchor / cohost / expert / narrator / ...
* ``sex``        male / female / other (informational)
* ``language``   ISO 639-1 code the voice speaks
* ``audio``      reference audio file name, relative to the voice directory
* ``transcript`` text file name, relative to the voice directory, holding the
                 EXACT words spoken in the reference (qwen3 clones in ICL mode)
* ``instruct``   optional VoiceDesign persona description
* ``enabled``    optional, default true

Since P6 the gateway owns the voice catalog, so this script never writes to a
local catalog: it talks to ``/voices/catalog`` through the shared client, then
registers the ``qwen3_tts`` clone. The run is a dry run by default: it validates
every directory and prints the result table without touching the gateway. Pass
``--apply`` to create the custom profiles, upload the references and clone the
voices.

Idempotent: an existing profile is reused, an existing reference is overwritten
with the same bytes, and an already-cloned ``<name>__qwen3_tts`` is not cloned
again.

Usage (from the repository root, so .env is found)::

    .venv/bin/python scripts/import_voice_dropin.py --dir ~/voice_dropin
    .venv/bin/python scripts/import_voice_dropin.py --dir ~/voice_dropin --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tts_contract import VoiceProfile, is_voice_slug  # noqa: E402

from src.api.services.gateway import gateway_headers  # noqa: E402
from src.api.services.gateway_voices import QWEN_ENGINE, clone_name_for  # noqa: E402
from src.api.services.voice_catalog_client import (  # noqa: E402
    fetch_catalog,
    fetch_reference_audio,
    save_custom_profile,
    store_reference,
)
from src.api.services.voices import VoicesService  # noqa: E402
from src.core.config import get_settings  # noqa: E402

#: Drop-in subdirectories starting with this prefix are templates, not voices.
EXAMPLE_PREFIX = "_"

#: Reference clips the gateway accepts, in seconds.
MIN_SECONDS = 5.0
MAX_SECONDS = 30.0

#: ``VoiceProfile.ref_text`` bounds enforced by the shared contract.
MIN_TRANSCRIPT = 100
MAX_TRANSCRIPT = 600

#: ``design_model`` recorded in the manifest for drop-in references.
DESIGN_MODEL = "dropin"


@dataclass
class VoiceSpec:
    """One parsed ``voice.json`` plus its resolved file paths.

    Attributes:
        directory: Subdirectory the spec was read from.
        name: Catalog profile name.
        label: Human label.
        role: Role hint.
        sex: Informational sex field.
        language: ISO 639-1 code.
        audio: Resolved reference audio path.
        transcript: Resolved transcript text path.
        instruct: Optional VoiceDesign description.
        enabled: Whether the imported profile is enabled.
        errors: Validation problems found while parsing.
        status: Outcome filled in when applying.
    """

    directory: Path
    name: str
    label: str
    role: str
    sex: str
    language: str
    audio: Path | None
    transcript: Path | None
    instruct: str
    enabled: bool
    errors: list[str] = field(default_factory=list)
    status: str = ""


def load_specs(root: Path) -> list[VoiceSpec]:
    """Read every voice directory under ``root`` into a :class:`VoiceSpec`.

    Args:
        root: Drop-in root directory.

    Returns:
        One spec per immediate subdirectory, excluding ``_``-prefixed
        templates, in sorted order. Unreadable directories yield a spec whose
        ``errors`` explains why.
    """
    specs: list[VoiceSpec] = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        if directory.name.startswith(EXAMPLE_PREFIX):
            continue
        specs.append(_load_one(directory))
    return specs


def _load_one(directory: Path) -> VoiceSpec:
    """Parse one voice directory, collecting validation errors.

    Args:
        directory: Voice directory holding ``voice.json``.

    Returns:
        The parsed spec; ``errors`` is non-empty when the directory is invalid.
    """
    spec = VoiceSpec(
        directory=directory,
        name="",
        label="",
        role="",
        sex="",
        language="",
        audio=None,
        transcript=None,
        instruct="",
        enabled=True,
    )
    manifest = directory / "voice.json"
    if not manifest.is_file():
        spec.errors.append("voice.json not found")
        return spec
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        spec.errors.append(f"voice.json is not readable JSON: {exc}")
        return spec
    if not isinstance(raw, dict):
        spec.errors.append("voice.json must be a JSON object")
        return spec

    spec.name = str(raw.get("name") or "").strip()
    spec.label = str(raw.get("label") or spec.name).strip()
    spec.role = str(raw.get("role") or "").strip()
    spec.sex = str(raw.get("sex") or "").strip().lower()
    spec.language = str(raw.get("language") or "").strip().lower()
    spec.instruct = str(raw.get("instruct") or "").strip()
    spec.enabled = bool(raw.get("enabled", True))

    if not is_voice_slug(spec.name):
        spec.errors.append(f"name must be a lowercase slug: {spec.name!r}")
    if len(spec.language) != 2 or not spec.language.isalpha():
        spec.errors.append(f"language must be a two-letter ISO 639-1 code: {spec.language!r}")

    spec.audio = _resolve_file(directory, raw.get("audio"), spec, "audio")
    spec.transcript = _resolve_file(directory, raw.get("transcript"), spec, "transcript")
    return spec


def _resolve_file(directory: Path, value: object, spec: VoiceSpec, field_name: str) -> Path | None:
    """Resolve a file field relative to the voice directory.

    Args:
        directory: Voice directory.
        value: File name declared in ``voice.json``.
        spec: Spec receiving an error when the file is missing.
        field_name: Field name for the error message.

    Returns:
        The resolved, existing path, or None when missing.
    """
    if not value:
        spec.errors.append(f"{field_name} file name is required")
        return None
    path = (directory / str(value)).resolve()
    if not path.is_relative_to(directory.resolve()):
        spec.errors.append(f"{field_name} must stay inside {directory.name}/")
        return None
    if not path.is_file():
        spec.errors.append(f"{field_name} file not found: {path.name}")
        return None
    return path


def read_transcript(spec: VoiceSpec) -> str:
    """Read and normalize the reference transcript.

    Args:
        spec: Spec whose ``transcript`` points at the text file.

    Returns:
        The transcript with surrounding whitespace collapsed.
    """
    assert spec.transcript is not None
    return " ".join(spec.transcript.read_text(encoding="utf-8").split())


def probe_duration(path: Path) -> float:
    """Return the duration of an audio file in seconds.

    Reads WAV headers with the stdlib and falls back to ``ffprobe`` for other
    containers.

    Args:
        path: Audio file.

    Returns:
        Duration in seconds, or -1.0 when it cannot be read.
    """
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            if rate:
                return handle.getnframes() / rate
    except (wave.Error, EOFError):
        pass

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return -1.0
    try:
        return float(result.stdout.strip())
    except ValueError:
        return -1.0


def validate(spec: VoiceSpec) -> None:
    """Append validation errors for a spec whose files resolve.

    Args:
        spec: Spec to validate in place.
    """
    if spec.errors or spec.audio is None:
        return
    duration = probe_duration(spec.audio)
    if duration < 0:
        spec.errors.append("reference audio is not readable")
    elif not (MIN_SECONDS <= duration <= MAX_SECONDS):
        spec.errors.append(
            f"reference audio must be {MIN_SECONDS:.0f}-{MAX_SECONDS:.0f}s: {duration:.1f}s"
        )
    transcript = read_transcript(spec)
    if not (MIN_TRANSCRIPT <= len(transcript) <= MAX_TRANSCRIPT):
        spec.errors.append(
            f"transcript must be {MIN_TRANSCRIPT}-{MAX_TRANSCRIPT} chars: {len(transcript)}"
        )
    instruct = build_instruct(spec)
    if not (10 <= len(instruct) <= 2000):
        spec.errors.append(f"instruct must be 10-2000 chars: {len(instruct)}")


def build_instruct(spec: VoiceSpec) -> str:
    """Return the profile instruct, generated from the metadata when omitted.

    Args:
        spec: Parsed spec.

    Returns:
        The explicit instruct, or a short persona description.
    """
    if spec.instruct:
        return spec.instruct
    return f"Voz {spec.sex or 'neutra'} en {spec.language} para el rol {spec.role or 'locucion'}."


def build_profile(spec: VoiceSpec) -> VoiceProfile:
    """Build the catalog profile for one valid spec.

    Args:
        spec: Validated spec.

    Returns:
        The profile to persist in the gateway's custom catalog.
    """
    transcript = read_transcript(spec)
    return VoiceProfile(
        name=spec.name,
        label=spec.label or spec.name,
        language=spec.language,
        instruct=build_instruct(spec),
        ref_text=transcript,
        ref_segments=[transcript],
        role_hint=spec.role,
        enabled=spec.enabled,
    )


async def _delete_clone(voice_id: str) -> None:
    """Delete a cloned voice from the gateway so it can be registered again.

    Args:
        voice_id: UUID of the clone to delete. A clone that is already gone is
            not an error.
    """
    settings = get_settings()
    url = f"{settings.tts_api_base_url.rstrip('/')}/voices/cloned/{voice_id}"
    async with httpx.AsyncClient(timeout=settings.api_request_timeout) as client:
        response = await client.delete(url, headers=gateway_headers(settings.tts_api_key))
    if response.status_code not in (204, 404):
        response.raise_for_status()


async def import_spec(
    spec: VoiceSpec,
    service: VoicesService,
    *,
    existing_profiles: set[str],
    existing_clones: dict[str, str],
    clone: bool,
    reclone: bool,
) -> None:
    """Create, upload and (optionally) clone one voice in the gateway.

    Args:
        spec: Validated spec.
        service: Gateway voices client.
        existing_profiles: Profile names already in the catalog.
        existing_clones: Gateway clone name to clone UUID.
        clone: Whether to register the ``qwen3_tts`` clone.
        reclone: Delete an existing clone and register it again, so a corrected
            ``ref_text`` reaches the engine.
    """
    assert spec.audio is not None
    profile = build_profile(spec)
    wav = spec.audio.read_bytes()

    if spec.name in existing_profiles:
        spec.status = "profile=existing"
    else:
        await save_custom_profile(profile)
        spec.status = "profile=created"

    entry = await store_reference(
        spec.name,
        wav,
        instruct=profile.instruct,
        ref_text=profile.ref_text,
        design_model=DESIGN_MODEL,
    )
    spec.status += f" ref={entry.duration_seconds:.1f}s"

    if not clone:
        return

    clone_name = clone_name_for(spec.name)
    existing_id = existing_clones.get(clone_name)
    if existing_id and not reclone:
        spec.status += f" clone=existing({clone_name})"
        return
    if existing_id:
        await _delete_clone(existing_id)
        spec.status += " clone=deleted"

    response = await service.clone_voice(
        name=clone_name,
        language=spec.language,
        reference_audio=wav,
        filename=spec.audio.name,
        ref_text=profile.ref_text,
        engines=QWEN_ENGINE,
    )
    voice_id = str(response.get("voice_id") or "")
    spec.status += f" clone={voice_id or '<none>'}"


async def run(root: Path, apply: bool, clone: bool, reclone: bool = False) -> int:
    """Load, validate and optionally import every voice in the drop-in root.

    Args:
        root: Drop-in root directory.
        apply: Whether to create profiles, upload references and clone.
        clone: Whether to register the ``qwen3_tts`` clone when applying.
        reclone: Delete and re-register existing clones.

    Returns:
        Process exit code: 0 when every valid spec imported, 1 otherwise.
    """
    if not root.is_dir():
        print(f"drop-in directory not found: {root}", file=sys.stderr)
        return 1

    specs = load_specs(root)
    if not specs:
        print(f"no voice directories under {root} (ignoring {EXAMPLE_PREFIX!r} templates)")
        return 1
    for spec in specs:
        validate(spec)

    if apply:
        service = VoicesService()
        catalog = await fetch_catalog(max_age=0)
        existing_profiles = {profile.name for profile in catalog.profiles}
        clones = await service.list_cloned(force=True)
        existing_clones = {
            str(entry.get("name") or ""): str(entry.get("id") or entry.get("voice_id") or "")
            for entry in clones
        }
        for spec in specs:
            if spec.errors:
                continue
            try:
                await import_spec(
                    spec,
                    service,
                    existing_profiles=existing_profiles,
                    existing_clones=existing_clones,
                    clone=clone,
                    reclone=reclone,
                )
                missing = await fetch_reference_audio(spec.name)
                if missing is None:
                    spec.status += " WARN=reference-missing"
            except Exception as exc:  # noqa: BLE001 - report per voice
                spec.errors.append(f"import failed: {exc}")

    print_report(specs)
    failed = sum(1 for spec in specs if spec.errors)
    mode = "APPLIED" if apply else "DRY RUN"
    print(f"{mode}: {len(specs) - failed}/{len(specs)} valid, {failed} failed")
    return 0 if failed == 0 else 1


def print_report(specs: list[VoiceSpec]) -> None:
    """Print the outcome table for every spec.

    Args:
        specs: Parsed specs.
    """
    print(f"{'VOICE':24} {'LANG':5} {'ROLE':10} {'STATUS':9} RESULT")
    print("-" * 100)
    for spec in specs:
        status = "ERROR" if spec.errors else "OK"
        if spec.errors:
            result = "; ".join(spec.errors)
        elif spec.status:
            result = spec.status
        else:
            assert spec.audio is not None
            result = f"{spec.audio.name} -> catalog refs/{spec.name}.wav"
        name = spec.name or spec.directory.name
        print(f"{name:24} {spec.language:5} {spec.role:10} {status:9} {result}")


def main() -> None:
    """Parse arguments and run the importer."""
    parser = argparse.ArgumentParser(description="Import voices from a drop-in directory")
    parser.add_argument("--dir", default="~/voice_dropin", help="Drop-in root directory")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create profiles, upload references and clone in the gateway",
    )
    parser.add_argument(
        "--no-clone",
        action="store_true",
        help="With --apply, skip the qwen3_tts clone registration",
    )
    parser.add_argument(
        "--reclone",
        action="store_true",
        help="With --apply, delete and register existing clones again",
    )
    args = parser.parse_args()
    root = Path(args.dir).expanduser()
    code = asyncio.run(run(root, apply=args.apply, clone=not args.no_clone, reclone=args.reclone))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
