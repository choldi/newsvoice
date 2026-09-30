"""Unit tests for the voice catalog loader and drift detection."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.api.models.voices import VoiceManifestEntry, VoiceProfile, VoiceSource
from src.core.voice_catalog import (
    VoiceCatalogError,
    is_stale,
    load_manifest,
    load_profiles,
    missing_or_stale,
    profile_dict,
    ref_text_for,
    resolve_reference,
    sha256_text,
    write_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPO_ROOT / "voice_profiles.json"

PROFILE = {
    "name": "anchor_neutral_m",
    "label": "Anchor — neutral male",
    "language": "en",
    "instruct": "Male, late 40s, warm baritone, broadcast clarity",
    "ref_text": "Welcome to the daily briefing. " * 6,
    "role_hint": "anchor",
    "enabled": True,
}


def _write_catalog(tmp_path: Path, profiles: list[dict]) -> Path:
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"version": 1, "profiles": profiles}), encoding="utf-8")
    return path


def _entry(**overrides: object) -> VoiceManifestEntry:
    payload = {
        "file": "refs/anchor_neutral_m.wav",
        "sha256": "a" * 64,
        "sample_rate": 24000,
        "duration_seconds": 15.0,
        "design_model": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        "instruct_sha256": "b" * 64,
        "ref_text_sha256": "c" * 64,
        "created_at": "2026-09-30T10:00:00+00:00",
        "modal_cost_usd": 0.01,
    }
    payload.update(overrides)
    return VoiceManifestEntry.model_validate(payload)


# --- load_profiles ---------------------------------------------------------


def test_load_profiles_returns_validated_profiles(tmp_path: Path) -> None:
    path = _write_catalog(tmp_path, [PROFILE])
    profiles = load_profiles(path)
    assert len(profiles) == 1
    assert profiles[0].name == "anchor_neutral_m"
    assert profiles[0].language == "en"


def test_load_profiles_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(VoiceCatalogError, match="not found"):
        load_profiles(tmp_path / "nope.json")


def test_load_profiles_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "profiles.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(VoiceCatalogError, match="not readable JSON"):
        load_profiles(path)


def test_load_profiles_rejects_missing_profiles_list(tmp_path: Path) -> None:
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"version": 1}), encoding="utf-8")
    with pytest.raises(VoiceCatalogError, match="profiles"):
        load_profiles(path)


def test_load_profiles_rejects_duplicate_names(tmp_path: Path) -> None:
    path = _write_catalog(tmp_path, [PROFILE, dict(PROFILE)])
    with pytest.raises(VoiceCatalogError, match="duplicate"):
        load_profiles(path)


def test_load_profiles_rejects_bad_language(tmp_path: Path) -> None:
    path = _write_catalog(tmp_path, [dict(PROFILE, language="eng")])
    with pytest.raises(ValidationError):
        load_profiles(path)


def test_load_profiles_rejects_bad_slug(tmp_path: Path) -> None:
    path = _write_catalog(tmp_path, [dict(PROFILE, name="Not A Slug!")])
    with pytest.raises(ValidationError):
        load_profiles(path)


def test_repo_catalog_loads_and_is_balanced() -> None:
    profiles = load_profiles(CATALOG)
    assert len(profiles) == 12
    english = [p for p in profiles if p.language == "en"]
    spanish = [p for p in profiles if p.language == "es"]
    assert len(english) == 8
    assert len(spanish) == 4
    assert all(p.enabled for p in profiles)


# --- load_manifest ---------------------------------------------------------


def test_load_manifest_missing_returns_empty(tmp_path: Path) -> None:
    assert load_manifest(tmp_path / "manifest.json") == {}


def test_load_manifest_parses_entries(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps({"anchor_neutral_m": _entry().model_dump(mode="json")}),
        encoding="utf-8",
    )
    manifest = load_manifest(path)
    assert set(manifest) == {"anchor_neutral_m"}


def test_load_manifest_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("nope", encoding="utf-8")
    with pytest.raises(VoiceCatalogError, match="not readable JSON"):
        load_manifest(path)


# --- drift detection -------------------------------------------------------


def test_is_stale_false_when_digests_match() -> None:
    profile = VoiceProfile.model_validate(PROFILE)
    entry = _entry(
        instruct_sha256=sha256_text(profile.instruct),
        ref_text_sha256=sha256_text(profile.ref_text),
    )
    assert is_stale(profile, entry) is False


def test_is_stale_true_when_instruct_changed() -> None:
    profile = VoiceProfile.model_validate(PROFILE)
    entry = _entry(
        instruct_sha256=sha256_text("something else entirely"),
        ref_text_sha256=sha256_text(profile.ref_text),
    )
    assert is_stale(profile, entry) is True


def test_is_stale_true_when_ref_text_changed() -> None:
    profile = VoiceProfile.model_validate(PROFILE)
    entry = _entry(
        instruct_sha256=sha256_text(profile.instruct),
        ref_text_sha256=sha256_text("a different transcript"),
    )
    assert is_stale(profile, entry) is True


def test_missing_or_stale_lists_only_enabled_pending(tmp_path: Path) -> None:
    fresh = VoiceProfile.model_validate(PROFILE)
    stale = VoiceProfile.model_validate(dict(PROFILE, name="stale_voice"))
    disabled = VoiceProfile.model_validate(dict(PROFILE, name="off_voice", enabled=False))
    manifest = {
        "anchor_neutral_m": _entry(
            instruct_sha256=sha256_text(fresh.instruct),
            ref_text_sha256=sha256_text(fresh.ref_text),
        ),
        "stale_voice": _entry(
            file="refs/stale_voice.wav",
            instruct_sha256=sha256_text("old instruct"),
            ref_text_sha256=sha256_text(stale.ref_text),
        ),
    }
    pending = missing_or_stale([fresh, stale, disabled], manifest)
    assert pending == ["stale_voice"]


def test_missing_or_stale_includes_never_generated() -> None:
    profile = VoiceProfile.model_validate(PROFILE)
    assert missing_or_stale([profile], {}) == ["anchor_neutral_m"]


# --- resolve_reference -----------------------------------------------------


def test_resolve_reference_none_when_not_generated(tmp_path: Path) -> None:
    profile = VoiceProfile.model_validate(PROFILE)
    assert resolve_reference(profile, {}, tmp_path) is None


def test_resolve_reference_raises_when_stale(tmp_path: Path) -> None:
    profile = VoiceProfile.model_validate(PROFILE)
    manifest = {"anchor_neutral_m": _entry(instruct_sha256="0" * 64)}
    with pytest.raises(VoiceCatalogError, match="stale"):
        resolve_reference(profile, manifest, tmp_path)


def test_resolve_reference_returns_path(tmp_path: Path) -> None:
    profile = VoiceProfile.model_validate(PROFILE)
    manifest = {
        "anchor_neutral_m": _entry(
            instruct_sha256=sha256_text(profile.instruct),
            ref_text_sha256=sha256_text(profile.ref_text),
        )
    }
    assert resolve_reference(profile, manifest, tmp_path) == tmp_path / "refs/anchor_neutral_m.wav"


# --- manifest round-trip ---------------------------------------------------


def test_write_manifest_round_trip(tmp_path: Path) -> None:
    entry = _entry()
    write_manifest({"anchor_neutral_m": entry}, tmp_path / "manifest.json")
    loaded = load_manifest(tmp_path / "manifest.json")
    assert loaded["anchor_neutral_m"].sha256 == entry.sha256


# --- helpers ---------------------------------------------------------------


def test_ref_text_for_returns_profile_transcript() -> None:
    profile = VoiceProfile.model_validate(PROFILE)
    assert ref_text_for(profile) == profile.ref_text


def test_profile_dict_is_json_serializable() -> None:
    profile = VoiceProfile.model_validate(PROFILE)
    assert profile_dict(profile)["name"] == "anchor_neutral_m"


def test_voice_source_values() -> None:
    assert VoiceSource.QWEN_DESIGN == "qwen_design"
    assert VoiceSource.RECORDING == "recording"
