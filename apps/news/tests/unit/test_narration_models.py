"""Unit tests for the narration domain models (Phase 7)."""

import pytest
from pydantic import ValidationError

from src.api.models.narration import (
    AUDIO_JOB_KINDS,
    NARRATION_KIND,
    TTS_KIND,
    MusicBed,
    NarrationScript,
    NarrationSegment,
    NarrationSpec,
    SpeakerRole,
)


def _spec(**overrides: object) -> NarrationSpec:
    """Build a minimal valid spec for tests."""
    payload: dict[str, object] = {
        "roles": [
            {"name": "anchor", "voice_id": "es-ES-ElviraNeural"},
            {"name": "expert", "voice_id": "es-ES-AlvaroNeural", "speed": 1.1},
        ]
    }
    payload.update(overrides)
    return NarrationSpec.model_validate(payload)


def test_audio_job_kinds_cover_tts_and_narration() -> None:
    assert AUDIO_JOB_KINDS == (TTS_KIND, NARRATION_KIND)


def test_spec_defaults_match_the_gateway_contract() -> None:
    spec = _spec()

    assert spec.language == "es"
    assert spec.pause_ms == 400
    assert spec.max_chars_per_request == 1500
    assert spec.music is None
    assert spec.speed == 1.0
    assert len(spec.roles) == 2


def test_spec_requires_at_least_one_role() -> None:
    with pytest.raises(ValidationError):
        _spec(roles=[])


def test_spec_rejects_duplicate_role_names() -> None:
    with pytest.raises(ValidationError, match="duplicate role names"):
        _spec(
            roles=[
                {"name": "anchor", "voice_id": "a"},
                {"name": "anchor", "voice_id": "b"},
            ]
        )


def test_spec_role_lookup_by_name() -> None:
    spec = _spec()

    assert spec.role("expert").voice_id == "es-ES-AlvaroNeural"
    with pytest.raises(KeyError):
        spec.role("ghost")


def test_spec_normalizes_language_and_emotion() -> None:
    spec = _spec(language="ES", emotion="Calm")

    assert spec.language == "es"
    assert spec.emotion == "calm"


@pytest.mark.parametrize("language", ["es-MX", "e", "español", "1a"])
def test_spec_rejects_non_iso639_1_language(language: str) -> None:
    with pytest.raises(ValidationError, match="two-letter ISO 639-1"):
        _spec(language=language)


def test_spec_rejects_unknown_emotion() -> None:
    with pytest.raises(ValidationError, match="emotion must be one of"):
        _spec(emotion="excitedly")


@pytest.mark.parametrize("pause", [-1, 5001])
def test_spec_rejects_out_of_range_pause(pause: int) -> None:
    with pytest.raises(ValidationError):
        _spec(pause_ms=pause)


@pytest.mark.parametrize("speed", [0.49, 2.01])
def test_spec_rejects_out_of_range_speed(speed: float) -> None:
    with pytest.raises(ValidationError):
        _spec(speed=speed)


def test_role_rejects_out_of_range_speed() -> None:
    with pytest.raises(ValidationError):
        SpeakerRole(name="anchor", voice_id="v", speed=3.0)


def test_role_language_override_is_validated() -> None:
    assert SpeakerRole(name="a", voice_id="v", language="en").language == "en"
    with pytest.raises(ValidationError, match="two-letter ISO 639-1"):
        SpeakerRole(name="a", voice_id="v", language="en-GB")


def test_role_requires_voice_id() -> None:
    with pytest.raises(ValidationError):
        SpeakerRole(name="anchor", voice_id="")


def test_music_bed_validation() -> None:
    bed = MusicBed(track="newsroom_intro.mp3", volume_db=-21.0)

    assert bed.fade_in_ms == 800
    assert bed.fade_out_ms == 1200
    assert bed.duck_db == -9.0

    with pytest.raises(ValidationError):
        MusicBed(track="loud.mp3", volume_db=3.0)
    with pytest.raises(ValidationError):
        MusicBed(track="")


def test_script_defaults() -> None:
    script = NarrationScript.model_validate(
        {"segments": [{"role": "anchor", "text": "Buenos dias"}]}
    )

    assert script.source == "llm"
    assert script.language == "es"
    assert script.title == ""
    assert script.total_chars() == len("Buenos dias")


def test_script_requires_segments() -> None:
    with pytest.raises(ValidationError):
        NarrationScript(segments=[])


def test_script_rejects_unknown_source() -> None:
    with pytest.raises(ValidationError, match="source must be one of"):
        NarrationScript.model_validate(
            {"source": "carrier-pigeon", "segments": [{"role": "anchor", "text": "hola"}]}
        )


def test_script_accepts_manual_and_api_sources() -> None:
    for source in ("manual", "api"):
        script = NarrationScript.model_validate(
            {"source": source, "segments": [{"role": "anchor", "text": "hola"}]}
        )
        assert script.source == source


def test_script_validates_segments_against_the_spec_when_context_provided() -> None:
    spec = _spec()
    payload = {"segments": [{"role": "anchor", "text": "hola"}]}

    assert NarrationScript.model_validate(payload, context={"spec": spec})

    with pytest.raises(ValidationError, match="unknown roles"):
        NarrationScript.model_validate(
            {"segments": [{"role": "ghost", "text": "hola"}]}, context={"spec": spec}
        )


def test_script_without_context_skips_role_check() -> None:
    script = NarrationScript.model_validate({"segments": [{"role": "ghost", "text": "hola"}]})

    assert script.segments[0].role == "ghost"


def test_segment_emotion_and_pause_are_optional_and_validated() -> None:
    segment = NarrationSegment(role="expert", text="ojo", emotion="Nervous", pause_after_ms=1200)

    assert segment.emotion == "nervous"
    assert segment.pause_after_ms == 1200

    with pytest.raises(ValidationError, match="emotion must be one of"):
        NarrationSegment(role="expert", text="ojo", emotion="bored")
    with pytest.raises(ValidationError):
        NarrationSegment(role="expert", text="ojo", pause_after_ms=9000)
    with pytest.raises(ValidationError):
        NarrationSegment(role="expert", text="")


def test_script_round_trips_through_json() -> None:
    script = NarrationScript.model_validate(
        {
            "title": "Edicion del dia",
            "language": "es",
            "source": "manual",
            "segments": [
                {"role": "anchor", "text": "Bienvenidos"},
                {"role": "expert", "text": "Gracias", "emotion": "calm"},
            ],
        }
    )

    restored = NarrationScript.model_validate_json(script.model_dump_json())

    assert restored == script
    assert restored.total_chars() == script.total_chars()
