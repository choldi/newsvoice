"""Unit tests for the voice catalog loader and drift detection."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.api.models.voices import VoiceManifestEntry, VoiceProfile, VoiceSource
from src.core.config import settings
from src.core.voice_catalog import (
    VoiceCatalogError,
    custom_profiles_path,
    is_stale,
    load_custom_profiles,
    load_manifest,
    load_profiles,
    missing_or_stale,
    profile_dict,
    ref_text_for,
    resolve_reference,
    save_custom_profile,
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


def _write_catalog(tmp_path: Path, profiles: list[dict], languages: dict | None = None) -> Path:
    path = tmp_path / "profiles.json"
    payload: dict = {"version": 2, "profiles": profiles}
    if languages is not None:
        payload["languages"] = languages
    path.write_text(json.dumps(payload), encoding="utf-8")
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
    assert len(profiles) == 32
    english = [p for p in profiles if p.language == "en"]
    spanish = [p for p in profiles if p.language == "es"]
    french = [p for p in profiles if p.language == "fr"]
    assert len(english) == 16
    assert len(spanish) == 8
    assert len(french) == 8
    assert all(p.enabled for p in profiles)


def test_repo_catalog_profiles_inherit_their_language_segments() -> None:
    """The reference transcript is written once per language, not per profile."""
    profiles = load_profiles(CATALOG)
    by_language: dict[str, set[tuple[str, ...]]] = {}
    for profile in profiles:
        by_language.setdefault(profile.language, set()).add(tuple(profile.ref_segments))

    assert all(len(segments) == 1 for segments in by_language.values())
    assert {len(segments.pop()) for segments in by_language.values()} == {2}


def test_load_profiles_inherits_the_language_block(tmp_path: Path) -> None:
    """A profile with no text of its own takes the shared segments."""
    first = "Shared first half of the reference transcript used by this test"
    second = "Shared second half of the reference transcript used by this test"
    textless = {k: v for k, v in PROFILE.items() if k != "ref_text"}
    path = _write_catalog(tmp_path, [textless], languages={"en": {"ref_segments": [first, second]}})

    profile = load_profiles(path)[0]

    assert profile.ref_segments == [first, second]
    assert profile.ref_text == f"{first} {second}"


def test_load_profiles_rejects_a_profile_without_any_reference_text(tmp_path: Path) -> None:
    path = _write_catalog(tmp_path, [{k: v for k, v in PROFILE.items() if k != "ref_text"}])

    with pytest.raises(VoiceCatalogError, match="has no reference text"):
        load_profiles(path)


def test_load_profiles_rejects_a_ref_text_that_disagrees_with_the_segments(
    tmp_path: Path,
) -> None:
    path = _write_catalog(
        tmp_path,
        [{**PROFILE, "ref_segments": ["first half", "second half"]}],
    )

    with pytest.raises(ValidationError, match="ref_text must be the ref_segments"):
        load_profiles(path)


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


# --- load_custom_profiles --------------------------------------------------


def test_load_custom_profiles_returns_empty_when_the_file_is_missing(
    tmp_path: Path,
) -> None:
    assert load_custom_profiles(tmp_path / "custom_profiles.json") == []


def test_custom_profiles_path_honours_an_explicit_path(tmp_path: Path) -> None:
    assert custom_profiles_path(tmp_path / "mine.json") == tmp_path / "mine.json"


def test_load_custom_profiles_returns_validated_profiles(tmp_path: Path) -> None:
    path = tmp_path / "custom_profiles.json"
    path.write_text(json.dumps({"profiles": [PROFILE]}), encoding="utf-8")

    profiles = load_custom_profiles(path)

    assert [profile.name for profile in profiles] == ["anchor_neutral_m"]


def test_load_custom_profiles_keeps_its_own_reference_text(tmp_path: Path) -> None:
    """A custom profile is not rewritten by the committed language block."""
    custom = dict(PROFILE, name="custom_es", language="es")
    path = tmp_path / "custom_profiles.json"
    path.write_text(json.dumps({"profiles": [custom]}), encoding="utf-8")

    profile = load_custom_profiles(path)[0]

    assert profile.name == "custom_es"
    assert profile.ref_text == PROFILE["ref_text"]


def test_load_custom_profiles_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "custom_profiles.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(VoiceCatalogError, match="not readable JSON"):
        load_custom_profiles(path)


def test_load_custom_profiles_requires_a_profiles_list(tmp_path: Path) -> None:
    path = tmp_path / "custom_profiles.json"
    path.write_text(json.dumps({"voices": []}), encoding="utf-8")

    with pytest.raises(VoiceCatalogError, match="needs a 'profiles' list"):
        load_custom_profiles(path)


def test_load_custom_profiles_rejects_duplicate_names(tmp_path: Path) -> None:
    path = tmp_path / "custom_profiles.json"
    path.write_text(json.dumps({"profiles": [PROFILE, PROFILE]}), encoding="utf-8")

    with pytest.raises(VoiceCatalogError, match="duplicate custom voice profile names"):
        load_custom_profiles(path)


def test_load_custom_profiles_rejects_an_invalid_profile(tmp_path: Path) -> None:
    broken = dict(PROFILE, language="spanish")
    path = tmp_path / "custom_profiles.json"
    path.write_text(json.dumps({"profiles": [broken]}), encoding="utf-8")

    with pytest.raises(VoiceCatalogError, match="is invalid"):
        load_custom_profiles(path)


# --- merged load + save_custom_profile (Phase 9.3) ---------------------------


def _isolated_voices_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point settings.voices_dir at an empty temp dir and return it."""
    voices = tmp_path / "voices"
    voices.mkdir()
    monkeypatch.setattr(settings, "voices_dir", str(voices))
    return voices


def _custom(name: str = "mi_voz_es") -> VoiceProfile:
    """Build a valid operator-authored profile for the merge tests."""
    return VoiceProfile.model_validate(dict(PROFILE, name=name))


def test_load_profiles_merges_the_custom_file_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    voices = _isolated_voices_dir(tmp_path, monkeypatch)
    custom_profiles_path().write_text(
        json.dumps({"profiles": [_custom().model_dump(mode="json")]}), encoding="utf-8"
    )

    profiles = load_profiles()
    committed = load_profiles(CATALOG)

    assert [profile.name for profile in profiles] == [
        *[profile.name for profile in committed],
        "mi_voz_es",
    ]
    assert len(committed) == 32
    assert voices.is_dir()


def test_load_profiles_with_an_explicit_path_stays_committed_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_voices_dir(tmp_path, monkeypatch)
    custom_profiles_path().write_text(
        json.dumps({"profiles": [_custom().model_dump(mode="json")]}), encoding="utf-8"
    )

    names = [profile.name for profile in load_profiles(CATALOG)]

    assert len(names) == 32
    assert "mi_voz_es" not in names


def test_load_profiles_rejects_a_cross_file_duplicate_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_voices_dir(tmp_path, monkeypatch)
    committed_name = load_profiles(CATALOG)[0].name
    custom_profiles_path().write_text(
        json.dumps({"profiles": [_custom(committed_name).model_dump(mode="json")]}),
        encoding="utf-8",
    )

    with pytest.raises(VoiceCatalogError, match="duplicate voice profile names"):
        load_profiles()


def test_save_custom_profile_appends_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_voices_dir(tmp_path, monkeypatch)

    save_custom_profile(_custom())
    save_custom_profile(_custom("otra_voz"))

    assert [profile.name for profile in load_custom_profiles()] == [
        "mi_voz_es",
        "otra_voz",
    ]
    target = custom_profiles_path()
    assert not target.with_name(f"{target.name}.tmp").exists()


def test_save_custom_profile_rejects_an_existing_custom_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_voices_dir(tmp_path, monkeypatch)
    save_custom_profile(_custom())

    with pytest.raises(VoiceCatalogError, match="already exists"):
        save_custom_profile(_custom())


def test_save_custom_profile_rejects_a_committed_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_voices_dir(tmp_path, monkeypatch)
    committed_name = load_profiles(CATALOG)[0].name

    with pytest.raises(VoiceCatalogError, match="already in the catalog"):
        save_custom_profile(_custom(committed_name))


def test_save_custom_profile_never_clobbers_a_corrupt_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_voices_dir(tmp_path, monkeypatch)
    target = custom_profiles_path()
    target.write_text("{not json", encoding="utf-8")

    with pytest.raises(VoiceCatalogError, match="not readable JSON"):
        save_custom_profile(_custom())

    assert target.read_text(encoding="utf-8") == "{not json"
