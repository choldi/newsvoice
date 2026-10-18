"""Unit tests for the catalog administration service (Phase 9.3)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.api.models.voices import CustomVoiceIn, VoiceProfile
from src.api.services import voice_catalog_admin
from src.core.config import settings
from src.core.voice_catalog import (
    VoiceCatalogError,
    load_custom_profiles,
)
from src.workers.voice_workflows import VoiceCatalogWorkflow
from tests.fake_gateway import current_gateway, sample_wav, save_custom_profile

#: The committed catalog the gateway ships with, whose first name the clash
#: tests ask for.
CATALOG = Path(__file__).resolve().parents[3] / "gateway" / "voice_profiles.json"

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

    committed_name = load_profiles(CATALOG)[0].name

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


async def test_start_generate_is_fire_and_forget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start_generate enqueues the run and returns without waiting for it."""
    client = AsyncMock()
    client.execute_workflow = AsyncMock(
        side_effect=AssertionError("start_generate must not wait for completion")
    )

    async def fake_build_client(namespace: str | None = None) -> object:
        return client

    monkeypatch.setattr(voice_catalog_admin, "build_client", fake_build_client)

    workflow_id = await voice_catalog_admin.start_generate(names=["mi_voz_es"])

    assert workflow_id.startswith("voice-catalog-")
    client.start_workflow.assert_awaited_once()
    assert client.start_workflow.await_args.kwargs["task_queue"] == (
        settings.voice_catalog_task_queue
    )
    client.execute_workflow.assert_not_awaited()


# --- catalog_status / generated_voices -----------------------------------------


async def test_catalog_status_reports_generated_and_missing() -> None:
    generated, pending = _profile("generated_es"), _profile("pending_es")
    save_custom_profile(generated)
    save_custom_profile(pending)
    current_gateway().add_reference(
        generated.name,
        sample_wav(),
        instruct=generated.instruct,
        ref_text=generated.ref_text,
    )

    status = await voice_catalog_admin.catalog_status()

    assert status.generated == ["generated_es"]
    assert "pending_es" in status.missing
    assert "generated_es" not in status.missing
    assert status.statuses["generated_es"] == voice_catalog_admin.STATUS_GENERATED
    assert status.statuses["pending_es"] == voice_catalog_admin.STATUS_PENDING


async def test_catalog_status_reports_a_reference_that_drifted() -> None:
    drifted = _profile("drifted_es")
    save_custom_profile(drifted)
    current_gateway().add_reference(
        drifted.name,
        sample_wav(),
        instruct="Otra descripción de persona bastante más larga",
        ref_text=drifted.ref_text,
    )

    status = await voice_catalog_admin.catalog_status()

    assert status.statuses["drifted_es"] == voice_catalog_admin.STATUS_STALE
    assert "drifted_es" in status.stale
    assert status.generated == []


async def test_catalog_status_keeps_the_gateway_error() -> None:
    current_gateway().unavailable = 503

    with pytest.raises(VoiceCatalogError, match="gateway unavailable"):
        await voice_catalog_admin.catalog_status()


async def test_generated_voices_puts_custom_profiles_first() -> None:
    save_custom_profile(_profile("custom_es"))

    rows = await voice_catalog_admin.generated_voices()

    assert rows[0]["profile"].name == "custom_es"
    assert rows[0]["custom"] is True
    assert rows[0]["status"] == voice_catalog_admin.STATUS_PENDING
    assert rows[0]["badge"] == voice_catalog_admin.STATUS_BADGES[voice_catalog_admin.STATUS_PENDING]
    assert rows[1]["custom"] is False
    assert len(rows) == 33


def test_workflow_import_is_the_real_catalog_workflow() -> None:
    assert voice_catalog_admin.VoiceCatalogWorkflow is VoiceCatalogWorkflow


# --- reference_audio ------------------------------------------------------------


async def test_reference_audio_resolves_the_reference() -> None:
    profile = _profile()
    save_custom_profile(profile)
    wav = sample_wav()
    current_gateway().add_reference(
        profile.name,
        wav,
        instruct=profile.instruct,
        ref_text=profile.ref_text,
    )

    assert await voice_catalog_admin.reference_audio("mi_voz_es.wav") == wav
    assert await voice_catalog_admin.reference_audio("mi_voz_es") == wav


async def test_reference_audio_refuses_unsafe_or_unknown_names() -> None:
    save_custom_profile(_profile())

    assert await voice_catalog_admin.reference_audio("../secret.wav") is None
    assert await voice_catalog_admin.reference_audio("Bad Name.wav") is None
    assert await voice_catalog_admin.reference_audio("no_existe") is None
    assert await voice_catalog_admin.reference_audio("mi_voz_es.wav") is None


async def test_reference_audio_is_none_when_the_gateway_is_down() -> None:
    current_gateway().unavailable = 503

    assert await voice_catalog_admin.reference_audio("anchor_neutral_m") is None
