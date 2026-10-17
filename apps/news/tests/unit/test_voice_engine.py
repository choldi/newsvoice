"""Unit tests for the multi-engine voice guard (Phase 9.2)."""

from unittest.mock import patch

import pytest

from src.api.models.narration import SpeakerRole, TTSBackend
from src.api.services.voice_engine import (
    VoiceEngineError,
    assert_same_engine,
    assert_voice_engine,
    engines_for_voice,
    infer_backend,
)

MODAL_NAME = "anchor_neutral_m"


@pytest.fixture
def modal_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the Modal catalog contain exactly one known profile."""

    class _Profile:
        name = MODAL_NAME

    with (
        patch("src.core.voice_catalog.load_profiles", return_value=[_Profile()]),
        patch("src.core.voice_catalog.load_custom_profiles", return_value=[]),
    ):
        yield


# --- engines_for_voice -----------------------------------------------------


def test_edge_shortname_voices_both_network_engines() -> None:
    assert engines_for_voice("es-MX-JorgeNeural") == {"edge", "omni_voice"}


def test_gateway_stock_ids_are_reachable_from_both_network_engines() -> None:
    assert engines_for_voice("es-mx-male") == {"edge", "omni_voice"}


def test_clone_uuid_is_gateway_only() -> None:
    uuid = "123e4567-e89b-12d3-a456-426614174000"
    assert engines_for_voice(uuid) == {"omni_voice"}


def test_modal_catalog_names_select_all_modal_engines(modal_catalog: None) -> None:
    assert engines_for_voice(MODAL_NAME) == {"qwen3_tts", "zonos2", "voxcpm2"}


def test_unknown_and_empty_voices_are_wildcards() -> None:
    assert engines_for_voice("") == set()
    assert engines_for_voice("   ") == set()
    assert engines_for_voice("legacy_free_text") == set()


# --- assert_voice_engine ---------------------------------------------------


def test_assert_voice_engine_allows_a_matching_voice() -> None:
    assert_voice_engine("es-MX-JorgeNeural", TTSBackend.OMNIVOICE)
    assert_voice_engine("es-mx-male", "edge")


def test_assert_voice_engine_rejects_modal_voices_on_network_engines(
    modal_catalog: None,
) -> None:
    with pytest.raises(VoiceEngineError, match="belongs to engine"):
        assert_voice_engine(MODAL_NAME, "edge")
    with pytest.raises(VoiceEngineError, match="belongs to engine"):
        assert_voice_engine(MODAL_NAME, "omni_voice")


def test_assert_voice_engine_rejects_network_voices_on_modal_engines(
    modal_catalog: None,
) -> None:
    with pytest.raises(VoiceEngineError):
        assert_voice_engine("es-MX-JorgeNeural", "qwen3_tts")


def test_assert_voice_engine_rejects_a_clone_on_edge() -> None:
    with pytest.raises(VoiceEngineError):
        assert_voice_engine("123e4567-e89b-12d3-a456-426614174000", "edge")


def test_assert_voice_engine_skips_unknown_voices_and_engines() -> None:
    assert_voice_engine("who_knows", "edge")
    assert_voice_engine("es-mx-male", "")
    assert_voice_engine("es-mx-male", "not-an-engine")


def test_assert_voice_engine_accepts_the_gateway_alias_spelling() -> None:
    """``gateway`` is an operator-facing alias of omni_voice."""
    assert_voice_engine("123e4567-e89b-12d3-a456-426614174000", "gateway")


# --- assert_same_engine ----------------------------------------------------


def test_assert_same_engine_accepts_a_uniform_selection() -> None:
    assert_same_engine(["es-MX-JorgeNeural", "en-US-AriaNeural"])


def test_assert_same_engine_accepts_network_voices_in_any_combination() -> None:
    """Edge ShortNames and gateway stock ids interoperate, so they never mix."""
    assert_same_engine(["es-MX-JorgeNeural", "es-mx-male"])


def test_assert_same_engine_accepts_unknown_voices_as_wildcards() -> None:
    assert_same_engine(["es-mx-male", "legacy_name"])


def test_assert_same_engine_rejects_modal_plus_network(modal_catalog: None) -> None:
    with pytest.raises(VoiceEngineError, match="mixed TTS engines"):
        assert_same_engine([MODAL_NAME, "es-MX-JorgeNeural"])


def test_assert_same_engine_rejects_clone_plus_modal(modal_catalog: None) -> None:
    with pytest.raises(VoiceEngineError, match="mixed TTS engines"):
        assert_same_engine(["123e4567-e89b-12d3-a456-426614174000", MODAL_NAME])


# --- infer_backend ---------------------------------------------------------


def test_infer_backend_keeps_the_gateway_default_for_network_voices() -> None:
    assert infer_backend(["es-MX-JorgeNeural", "es-mx-male"]) == "omni_voice"


def test_infer_backend_selects_modal_for_catalog_voices(modal_catalog: None) -> None:
    assert infer_backend([MODAL_NAME]) == "qwen3_tts"


def test_infer_backend_returns_empty_when_nothing_is_classifiable() -> None:
    assert infer_backend(["free_text", ""]) == ""


def test_infer_backend_rejects_a_mixed_selection(modal_catalog: None) -> None:
    with pytest.raises(VoiceEngineError, match="mixed TTS engines"):
        infer_backend([MODAL_NAME, "es-MX-JorgeNeural"])


# --- NarrationSpec integration --------------------------------------------


def test_narration_spec_rejects_a_cast_from_another_engine() -> None:
    """The Pydantic validator turns the guard into a 422 for every client."""
    from pydantic import ValidationError

    from src.api.models.narration import NarrationSpec

    with pytest.raises(ValidationError, match="belongs to engine"):
        NarrationSpec.model_validate(
            {
                "language": "es",
                "roles": [{"name": "anchor", "voice_id": "es-MX-JorgeNeural"}],
                "tts_backend": "qwen3_tts",
            }
        )


def test_narration_spec_accepts_the_gateway_network_voices() -> None:
    from src.api.models.narration import NarrationSpec

    spec = NarrationSpec.model_validate(
        {
            "language": "es",
            "roles": [{"name": "anchor", "voice_id": "es-MX-JorgeNeural"}],
        }
    )
    assert spec.tts_backend is TTSBackend.OMNIVOICE
    assert isinstance(spec.roles[0], SpeakerRole)


def test_narration_spec_rejects_a_mixed_cast(modal_catalog: None) -> None:
    from pydantic import ValidationError

    from src.api.models.narration import NarrationSpec

    with pytest.raises(ValidationError, match="mixed TTS engines"):
        NarrationSpec.model_validate(
            {
                "language": "es",
                "roles": [
                    {"name": "a", "voice_id": MODAL_NAME},
                    {"name": "b", "voice_id": "es-MX-JorgeNeural"},
                ],
                "tts_backend": "qwen3_tts",
            }
        )
