"""Unit tests for the unified voice registry (Phase 9.1)."""

from unittest.mock import AsyncMock

import pytest

from src.api.models.voices import VoiceOption, VoiceProfile
from src.api.services import voice_registry


def _profile(name: str, language: str = "en") -> VoiceProfile:
    """Build a valid catalog profile for the modal source tests."""
    return VoiceProfile.model_validate(
        {
            "name": name,
            "label": name.replace("_", " ").title(),
            "language": language,
            "instruct": "Male, late 40s, warm baritone, broadcast clarity",
            "ref_text": "Welcome to the daily briefing. " * 6,
        }
    )


async def _empty_source() -> tuple[list[str], list[VoiceOption]]:
    """Return an empty (languages, voices) source."""
    return [], []


async def test_gateway_options_tags_stock_and_cloned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway entries arrive tagged with their engine and origin."""
    from src.api.services.voices import VoicesService

    monkeypatch.setattr(VoicesService, "list_languages", AsyncMock(return_value=["en", "es"]))
    monkeypatch.setattr(
        VoicesService,
        "list_stock",
        AsyncMock(
            return_value=[
                {"voice_id": "es-mx-male", "language": "es", "name": "Male ES"},
                {"voice_id": "en-us-female", "language": "en", "name": "Female US"},
            ]
        ),
    )
    monkeypatch.setattr(
        VoicesService,
        "list_cloned",
        AsyncMock(return_value=[{"voice_id": "uuid-1", "language": "es", "name": "mario"}]),
    )

    languages, voices = await voice_registry.gateway_options()

    assert languages == ["en", "es"]
    by_id = {voice.id: voice for voice in voices}
    assert by_id["es-mx-male"].category == "stock"
    assert by_id["es-mx-male"].engines == ["omni_voice"]
    assert by_id["uuid-1"].category == "cloned"
    assert by_id["uuid-1"].label == "mario"


async def test_gateway_options_degrade_when_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead gateway yields empty lists instead of raising."""
    from src.api.services.voices import VoicesService

    monkeypatch.setattr(
        VoicesService, "list_languages", AsyncMock(side_effect=RuntimeError("gateway down"))
    )

    languages, voices = await voice_registry.gateway_options()

    assert languages == []
    assert voices == []


async def test_edge_options_tags_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Edge ShortNames are offered as stock voices of the edge engine."""
    from src.api.services import tts as tts_service

    monkeypatch.setattr(
        tts_service,
        "_EDGE_VOICES",
        [{"ShortName": "es-ES-DelviraNeural", "Locale": "es-ES", "FriendlyName": "Elvira"}],
    )

    languages, voices = await voice_registry.edge_options()

    assert languages == ["es"]
    assert voices[0].id == "es-ES-DelviraNeural"
    assert voices[0].engines == ["edge"]
    assert voices[0].category == "stock"


def test_modal_options_separates_stock_and_generated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Committed profiles are stock; operator-created ones are generated."""
    import src.core.voice_catalog as voice_catalog

    monkeypatch.setattr(voice_catalog, "load_profiles", lambda path=None: [_profile("anchor_m")])
    monkeypatch.setattr(
        voice_catalog, "load_custom_profiles", lambda path=None: [_profile("custom_es", "es")]
    )

    languages, voices = voice_registry.modal_options()

    assert languages == ["en", "es"]
    by_id = {voice.id: voice for voice in voices}
    assert by_id["anchor_m"].category == "stock"
    assert by_id["custom_es"].category == "generated"
    assert by_id["custom_es"].engines == ["qwen3_tts", "zonos2"]


async def test_list_all_merges_sources_and_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same id offered by two engines is listed once with both engines."""

    async def _gateway() -> tuple[list[str], list[VoiceOption]]:
        return ["es"], [_option("shared-id", engines=["omni_voice"])]

    async def _edge() -> tuple[list[str], list[VoiceOption]]:
        return ["es"], [
            _option("shared-id", engines=["edge"]),
            _option("es-ES-DelviraNeural", engines=["edge"]),
        ]

    monkeypatch.setattr(voice_registry, "gateway_options", _gateway)
    monkeypatch.setattr(voice_registry, "edge_options", _edge)
    monkeypatch.setattr(voice_registry, "modal_options", lambda: ([], []))

    everything = await voice_registry.list_all_voices()

    assert sorted(voice.id for voice in everything) == [
        "es-ES-DelviraNeural",
        "shared-id",
    ]
    shared = next(voice for voice in everything if voice.id == "shared-id")
    assert sorted(shared.engines) == ["edge", "omni_voice"]


async def test_list_all_filters_by_engine_and_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filters narrow the listing by engine vocabulary and by origin."""

    async def _gateway() -> tuple[list[str], list[VoiceOption]]:
        return ["es"], [
            _option("es-mx-male", category="stock"),
            _option("uuid-clone", category="cloned"),
        ]

    def _modal() -> tuple[list[str], list[VoiceOption]]:
        return ["es"], [_option("custom_es", engines=["qwen3_tts", "zonos2"], category="generated")]

    monkeypatch.setattr(voice_registry, "edge_options", _empty_source)
    monkeypatch.setattr(voice_registry, "gateway_options", _gateway)
    monkeypatch.setattr(voice_registry, "modal_options", _modal)

    edge_only = await voice_registry.list_all_voices(engine="edge")
    generated = await voice_registry.list_all_voices(category="generated")
    modal_qwen = await voice_registry.list_all_voices(engine="qwen3_tts")
    gateway_stock = await voice_registry.list_all_voices(category="stock")

    assert edge_only == []
    assert [voice.id for voice in generated] == ["custom_es"]
    assert [voice.id for voice in modal_qwen] == ["custom_es"]
    assert [voice.id for voice in gateway_stock] == ["es-mx-male"]


def _option(
    identifier: str,
    *,
    language: str = "es",
    engines: list[str] | None = None,
    category: str = "stock",
) -> VoiceOption:
    """Build a VoiceOption with sensible defaults for the listing tests."""
    return VoiceOption(
        id=identifier,
        label=identifier,
        language=language,
        engines=engines if engines is not None else ["omni_voice"],
        category=category,
    )
