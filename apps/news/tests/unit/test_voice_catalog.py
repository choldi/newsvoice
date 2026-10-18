"""Unit tests for the voice catalog loader and drift detection."""

import json
from pathlib import Path

import pytest

from src.api.models.voices import VoiceManifestEntry, VoiceProfile, VoiceSource
from src.api.services import voice_catalog_client
from src.core.voice_catalog import (
    VoiceCatalogError,
    is_stale,
    load_custom_profiles,
    load_manifest,
    load_profiles,
    missing_or_stale,
    profile_dict,
    ref_text_for,
    resolve_reference,
    sha256_text,
    write_manifest,
)
from tests.fake_gateway import (
    current_gateway,
    invalidate_client,
    sample_wav,
    save_custom_profile,
)

#: The committed catalog, which the gateway ships and serves since P6.
CATALOG = Path(__file__).resolve().parents[3] / "gateway" / "voice_profiles.json"

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
    with pytest.raises(VoiceCatalogError, match="language must be a two-letter"):
        load_profiles(path)


def test_load_profiles_rejects_bad_slug(tmp_path: Path) -> None:
    path = _write_catalog(tmp_path, [dict(PROFILE, name="Not A Slug!")])
    with pytest.raises(VoiceCatalogError, match="is invalid"):
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

    with pytest.raises(VoiceCatalogError, match="ref_text must be the ref_segments"):
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


def test_load_custom_profiles_is_empty_when_the_operator_created_none() -> None:
    assert load_custom_profiles() == []


def test_load_custom_profiles_returns_the_seeded_profiles() -> None:
    save_custom_profile(VoiceProfile.model_validate(dict(PROFILE, name="mi_voz_es")))

    assert [profile.name for profile in load_custom_profiles()] == ["mi_voz_es"]


def test_load_custom_profiles_keeps_its_own_reference_text() -> None:
    """A custom profile is not rewritten by the committed language block."""
    save_custom_profile(VoiceProfile.model_validate(dict(PROFILE, name="custom_es", language="es")))

    profile = load_custom_profiles()[0]

    assert profile.name == "custom_es"
    assert profile.ref_text == PROFILE["ref_text"]


def test_load_custom_profiles_reports_a_gateway_that_cannot_be_asked() -> None:
    current_gateway().unavailable = 503

    with pytest.raises(VoiceCatalogError, match="gateway unavailable"):
        load_custom_profiles()


def test_load_profiles_keeps_the_gateway_duplicate_message() -> None:
    """A catalog the gateway cannot assemble surfaces its own wording."""
    committed_name = load_profiles(CATALOG)[0].name
    current_gateway().custom.append(VoiceProfile.model_validate(dict(PROFILE, name=committed_name)))
    invalidate_client()

    with pytest.raises(VoiceCatalogError, match="duplicate voice profile names"):
        load_profiles()


def test_load_profiles_reports_an_unreachable_gateway() -> None:
    current_gateway().unavailable = 503

    with pytest.raises(VoiceCatalogError, match="gateway unavailable"):
        load_profiles()


def test_load_manifest_without_a_path_serves_the_gateway_manifest() -> None:
    save_custom_profile(VoiceProfile.model_validate(dict(PROFILE, name="mi_voz_es")))
    current_gateway().add_reference(
        "mi_voz_es",
        sample_wav(),
        instruct=PROFILE["instruct"],
        ref_text=PROFILE["ref_text"],
    )

    manifest = load_manifest()

    assert set(manifest) == {"mi_voz_es"}
    assert manifest["mi_voz_es"].file == "refs/mi_voz_es.wav"


# --- merged load + save_custom_profile (Phase 9.3) ---------------------------


def _custom(name: str = "mi_voz_es") -> VoiceProfile:
    """Build a valid operator-authored profile for the merge tests."""
    return VoiceProfile.model_validate(dict(PROFILE, name=name))


def test_load_profiles_merges_the_custom_profiles_last() -> None:
    save_custom_profile(_custom())

    profiles = load_profiles()
    committed = load_profiles(CATALOG)

    assert [profile.name for profile in profiles] == [
        *[profile.name for profile in committed],
        "mi_voz_es",
    ]
    assert len(committed) == 32


def test_load_profiles_with_an_explicit_path_stays_committed_only() -> None:
    save_custom_profile(_custom())

    names = [profile.name for profile in load_profiles(CATALOG)]

    assert len(names) == 32
    assert "mi_voz_es" not in names


async def test_save_custom_profile_appends_through_the_gateway() -> None:
    seed = await voice_catalog_client.save_custom_profile(_custom())
    await voice_catalog_client.save_custom_profile(_custom("otra_voz"))

    assert seed.name == "mi_voz_es"
    assert [profile.name for profile in load_custom_profiles()] == [
        "mi_voz_es",
        "otra_voz",
    ]


async def test_save_custom_profile_rejects_an_existing_custom_name() -> None:
    await voice_catalog_client.save_custom_profile(_custom())

    with pytest.raises(VoiceCatalogError, match="already exists"):
        await voice_catalog_client.save_custom_profile(_custom())


async def test_save_custom_profile_rejects_a_committed_name() -> None:
    committed_name = load_profiles(CATALOG)[0].name

    with pytest.raises(VoiceCatalogError, match="already in the catalog"):
        await voice_catalog_client.save_custom_profile(_custom(committed_name))


async def test_save_custom_profile_marks_a_refusal_as_not_retryable() -> None:
    await voice_catalog_client.save_custom_profile(_custom())

    try:
        await voice_catalog_client.save_custom_profile(_custom())
    except voice_catalog_client.VoiceCatalogRequestError as exc:
        assert exc.retryable is False
    else:
        raise AssertionError("the duplicate must be refused")


async def test_save_custom_profile_marks_an_outage_as_retryable() -> None:
    current_gateway().unavailable = 503

    try:
        await voice_catalog_client.save_custom_profile(_custom())
    except voice_catalog_client.VoiceCatalogRequestError as exc:
        assert exc.retryable is True
    else:
        raise AssertionError("the outage must fail the save")
