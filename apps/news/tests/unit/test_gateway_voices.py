"""Unit tests for the local qwen3_tts gateway voice resolver (Phase 10.5)."""

from unittest.mock import AsyncMock

import pytest

from src.api.models.voices import VoiceProfile
from src.api.services import gateway_voices
from tests.fake_gateway import current_gateway, sample_wav, save_custom_profile


def _profile(name: str, language: str = "fr") -> VoiceProfile:
    """Build a valid catalog profile for the resolver tests."""
    return VoiceProfile.model_validate(
        {
            "name": name,
            "label": name,
            "language": language,
            "instruct": "Warm female teacher from the South of France",
            "ref_text": (
                "Les enfants attentifs achètent du pain frais, trois grands croissants "
                "chauds et un bon jus de fruits pour le goûter de ce matin."
            ),
        }
    )


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """Keep the module-level resolution cache from leaking across tests."""
    gateway_voices._RESOLVED.clear()


def _install_catalog(wav: bool = True) -> None:
    """Seed the fake gateway with a catalog profile and (or without) a reference."""
    profile = _profile("marie_fr")
    save_custom_profile(profile)
    if wav:
        current_gateway().add_reference(
            profile.name,
            sample_wav(),
            instruct=profile.instruct,
            ref_text=profile.ref_text,
        )


async def test_gateway_id_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gateway stock id or clone UUID is returned untouched."""
    service = AsyncMock()
    monkeypatch.setattr(gateway_voices, "VoicesService", lambda: service)

    assert await gateway_voices.resolve_gateway_qwen_voice("fr-fr-male", "fr") == "fr-fr-male"
    uuid = "3a1c2dd5-45f0-4ad3-a067-54f6fbac7765"
    assert await gateway_voices.resolve_gateway_qwen_voice(uuid, "fr") == uuid
    assert await gateway_voices.resolve_gateway_qwen_voice("", "fr") == ""
    service.list_cloned.assert_not_called()


async def test_profile_without_reference_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile with no generated WAV keeps its name (gateway will complain)."""
    _install_catalog(wav=False)
    service = AsyncMock()
    monkeypatch.setattr(gateway_voices, "VoicesService", lambda: service)

    assert await gateway_voices.resolve_gateway_qwen_voice("marie_fr", "fr") == "marie_fr"
    service.clone_voice.assert_not_called()


async def test_existing_qwen_clone_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A previously registered qwen3_tts clone is found without re-cloning."""
    _install_catalog()
    service = AsyncMock()
    service.list_cloned.return_value = [
        {"id": "uuid-pocket", "name": "marie_fr", "engine": "pocket_tts"},
        {"id": "uuid-qwen", "name": "marie_fr__qwen3_tts", "engine": "qwen3_tts"},
    ]
    monkeypatch.setattr(gateway_voices, "VoicesService", lambda: service)

    resolved = await gateway_voices.resolve_gateway_qwen_voice("marie_fr", "fr")

    assert resolved == "uuid-qwen"
    service.clone_voice.assert_not_called()


async def test_missing_clone_is_registered_and_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A designed voice is cloned into the gateway once and its id reused."""
    _install_catalog()
    service = AsyncMock()
    service.list_cloned.return_value = []
    service.clone_voice.return_value = {"voice_id": "uuid-new", "name": "marie_fr__qwen3_tts"}
    monkeypatch.setattr(gateway_voices, "VoicesService", lambda: service)

    first = await gateway_voices.resolve_gateway_qwen_voice("marie_fr", "fr")
    second = await gateway_voices.resolve_gateway_qwen_voice("marie_fr", "fr")

    assert first == "uuid-new"
    assert second == "uuid-new"
    service.clone_voice.assert_awaited_once()
    _, kwargs = service.clone_voice.call_args
    assert kwargs["name"] == "marie_fr__qwen3_tts"
    assert kwargs["engines"] == "qwen3_tts"
    assert kwargs["ref_text"] == _profile("marie_fr").ref_text
    service.list_cloned.assert_awaited_once()


async def test_clone_name_resolves_to_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    """A requested gateway clone name resolves to its UUID without cloning."""
    service = AsyncMock()
    service.list_cloned.return_value = [
        {"id": "uuid-pocket", "name": "marie_fr", "engine": "pocket_tts"},
        {"id": "uuid-qwen", "name": "marie_fr__qwen3_tts", "engine": "qwen3_tts"},
    ]
    monkeypatch.setattr(gateway_voices, "VoicesService", lambda: service)

    first = await gateway_voices.resolve_gateway_qwen_voice("marie_fr__qwen3_tts", "fr")
    second = await gateway_voices.resolve_gateway_qwen_voice("marie_fr__qwen3_tts", "fr")

    assert first == "uuid-qwen"
    assert second == "uuid-qwen"
    service.clone_voice.assert_not_called()
    service.list_cloned.assert_awaited_once()


async def test_missing_clone_name_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown gateway clone name is left for the gateway to reject loudly."""
    service = AsyncMock()
    service.list_cloned.return_value = [
        {"id": "uuid-pocket", "name": "marie_fr", "engine": "pocket_tts"},
    ]
    monkeypatch.setattr(gateway_voices, "VoicesService", lambda: service)

    resolved = await gateway_voices.resolve_gateway_qwen_voice("ghost__qwen3_tts", "fr")

    assert resolved == "ghost__qwen3_tts"
    assert "ghost__qwen3_tts" not in gateway_voices._RESOLVED
    service.clone_voice.assert_not_called()


async def test_gateway_failure_degrades_to_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable gateway leaves the voice name so the caller can fall back."""
    _install_catalog()
    service = AsyncMock()
    service.list_cloned.side_effect = RuntimeError("gateway down")
    monkeypatch.setattr(gateway_voices, "VoicesService", lambda: service)

    resolved = await gateway_voices.resolve_gateway_qwen_voice("marie_fr", "fr")

    assert resolved == "marie_fr"
    assert "marie_fr" not in gateway_voices._RESOLVED


def test_clone_name_is_engine_scoped() -> None:
    """The clone name is suffixed so it never clashes with another engine."""
    assert gateway_voices.clone_name_for("marie_fr") == "marie_fr__qwen3_tts"
