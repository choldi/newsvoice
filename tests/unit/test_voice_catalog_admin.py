"""Unit tests for the catalog administration service (Phase 9.3)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.api.models.voices import CustomVoiceIn, VoiceManifestEntry, VoiceProfile
from src.api.services import voice_catalog_admin
from src.core.config import settings
from src.core.voice_catalog import (
    VoiceCatalogError,
    custom_profiles_path,
    load_custom_profiles,
    save_custom_profile,
    sha256_text,
    write_manifest,
)
from src.workers.voice_workflows import VoiceCatalogWorkflow

REF_TEXT = "Hola, soy tu profe de ingles de siempre. " * 4


def _profile(name: str = "mi_voz_es", language: str = "es") -> VoiceProfile:
    """Build a valid catalog profile for the service tests."""
    return VoiceProfile.model_validate(
        {
            "name": name,
            "label": name.replace("_", " ").title(),
            "language": language,
            "instruct": "Femenina, 30s, tono cercano y agil, claridad de locucion",
            "ref_text": REF_TEXT,
        }
    )


def _payload(name: str = "mi_voz_es", **overrides: object) -> CustomVoiceIn:
    """Build a valid custom voice request."""
    payload: dict[str, object] = {
        "name": name,
        "label": "Mi voz",
        "language": "es",
        "instruct": "Femenina, 30s, tono cercano y agil, claridad de locucion",
        "ref_text": REF_TEXT,
        "generate": True,
    }
    payload.update(overrides)
    return CustomVoiceIn.model_validate(payload)


def _entry(profile: VoiceProfile, file: str = "refs/mi_voz_es.wav") -> VoiceManifestEntry:
    """Build a fresh manifest entry for a profile."""
    return VoiceManifestEntry(
        file=file,
        sha256="a" * 64,
        sample_rate=24000,
        duration_seconds=15.0,
        design_model="Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        instruct_sha256=sha256_text(profile.instruct),
        ref_text_sha256=sha256_text(profile.ref_text),
        created_at="2026-10-07T00:00:00Z",
    )


@pytest.fixture(autouse=True)
def _tmp_voices_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate every read/write of profiles and manifests per test."""
    voices = tmp_path / "voices"
    voices.mkdir()
    monkeypatch.setattr(settings, "voices_dir", str(voices))
    return voices


# --- create_custom_voice ------------------------------------------------------


async def test_create_custom_voice_saves_and_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = AsyncMock(return_value="voice-catalog-mi-voz-es")
    monkeypatch.setattr(voice_catalog_admin, "start_generate", start)

    result = await voice_catalog_admin.create_custom_voice(_payload())

    assert result["profile"].name == "mi_voz_es"
    assert result["workflow_id"] == "voice-catalog-mi-voz-es"
    assert result["start_error"] == ""
    assert [profile.name for profile in load_custom_profiles()] == ["mi_voz_es"]
    start.assert_awaited_once_with(names=["mi_voz_es"], upload=False)


async def test_create_custom_voice_without_generate_skips_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = AsyncMock(return_value="voice-catalog-should-not-run")
    monkeypatch.setattr(voice_catalog_admin, "start_generate", start)

    result = await voice_catalog_admin.create_custom_voice(_payload(generate=False))

    assert result["workflow_id"] == ""
    assert result["start_error"] == ""
    start.assert_not_awaited()
    assert [profile.name for profile in load_custom_profiles()] == ["mi_voz_es"]


async def test_create_custom_voice_keeps_the_profile_when_enqueue_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(names: list[str] | None = None, upload: bool = False) -> str:
        raise voice_catalog_admin.VoiceCatalogAdminError("temporal down")

    monkeypatch.setattr(voice_catalog_admin, "start_generate", boom)

    result = await voice_catalog_admin.create_custom_voice(_payload())

    assert result["profile"].name == "mi_voz_es"
    assert result["workflow_id"] == ""
    assert "temporal down" in result["start_error"]
    assert [profile.name for profile in load_custom_profiles()] == ["mi_voz_es"]


async def test_create_custom_voice_rejects_an_existing_custom_name() -> None:
    save_custom_profile(_profile())

    with pytest.raises(VoiceCatalogError, match="already exists"):
        await voice_catalog_admin.create_custom_voice(_payload())


async def test_create_custom_voice_rejects_a_committed_name() -> None:
    from src.core.voice_catalog import load_profiles

    committed_name = load_profiles("voice_profiles.json")[0].name

    with pytest.raises(VoiceCatalogError, match="already in the catalog"):
        await voice_catalog_admin.create_custom_voice(_payload(name=committed_name))


async def test_create_custom_voice_rejects_a_short_reference() -> None:
    with pytest.raises(ValidationError):
        await voice_catalog_admin.create_custom_voice(_payload(ref_text="too short"))

    assert load_custom_profiles() == []


# --- start_generate -----------------------------------------------------------


async def test_start_generate_wraps_temporal_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("temporal down")

    monkeypatch.setattr(voice_catalog_admin, "build_client", boom)

    with pytest.raises(voice_catalog_admin.VoiceCatalogAdminError, match="temporal down"):
        await voice_catalog_admin.start_generate(names=["mi_voz_es"])


# --- generated_status / catalog_status / generated_voices ---------------------


def test_generated_status_classifies_the_reference(
    tmp_path: Path,
) -> None:
    profile = _profile()
    manifest: dict[str, VoiceManifestEntry] = {}

    assert (
        voice_catalog_admin.generated_status(profile, manifest, tmp_path)
        == voice_catalog_admin.STATUS_PENDING
    )

    fresh = _entry(profile)
    manifest[profile.name] = fresh
    wav = tmp_path / fresh.file
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"RIFF....WAVE")

    assert (
        voice_catalog_admin.generated_status(profile, manifest, tmp_path)
        == voice_catalog_admin.STATUS_GENERATED
    )

    wav.unlink()
    assert (
        voice_catalog_admin.generated_status(profile, manifest, tmp_path)
        == voice_catalog_admin.STATUS_PENDING
    )

    stale = _entry(profile)
    stale.instruct_sha256 = "0" * 64
    manifest[profile.name] = stale
    assert (
        voice_catalog_admin.generated_status(profile, manifest, tmp_path)
        == voice_catalog_admin.STATUS_STALE
    )


async def test_catalog_status_reports_generated_and_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated, stale = _profile("generated_es"), _profile("stale_es")
    monkeypatch.setattr(voice_catalog_admin, "load_profiles", lambda: [generated, stale])
    wav = tmp_path / "voices" / "refs" / "generated_es.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"RIFF....WAVE")
    stale_entry = _entry(stale, file="refs/stale_es.wav")
    stale_entry.ref_text_sha256 = "0" * 64
    write_manifest(
        {
            "generated_es": _entry(generated),
            "stale_es": stale_entry,
        },
        voice_catalog_admin.manifest_path(),
    )

    status = await voice_catalog_admin.catalog_status()

    assert status["status"] == "ok"
    assert status["generated"] == ["generated_es"]
    assert status["missing"] == ["stale_es"]
    assert status["stale"] == ["stale_es"]


async def test_generated_voices_puts_custom_profiles_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stock, custom = _profile("stock_es"), _profile("custom_es")
    monkeypatch.setattr(voice_catalog_admin, "load_profiles", lambda: [stock, custom])
    save_custom_profile(custom)

    rows = await voice_catalog_admin.generated_voices()

    assert [row["profile"].name for row in rows] == ["custom_es", "stock_es"]
    assert rows[0]["custom"] is True
    assert rows[0]["status"] == voice_catalog_admin.STATUS_PENDING
    assert rows[1]["custom"] is False
    assert rows[0]["badge"] == voice_catalog_admin.STATUS_BADGES[voice_catalog_admin.STATUS_PENDING]


def test_manifest_path_and_refs_dir_follow_settings(tmp_path: Path) -> None:
    assert voice_catalog_admin.manifest_path() == tmp_path / "voices" / "manifest.json"
    assert voice_catalog_admin.refs_dir() == tmp_path / "voices" / "refs"


def test_custom_profiles_file_is_written_under_the_voices_dir() -> None:
    save_custom_profile(_profile())

    raw = json.loads(custom_profiles_path().read_text(encoding="utf-8"))
    assert [item["name"] for item in raw["profiles"]] == ["mi_voz_es"]


def test_workflow_import_is_the_real_catalog_workflow() -> None:
    assert voice_catalog_admin.VoiceCatalogWorkflow is VoiceCatalogWorkflow
