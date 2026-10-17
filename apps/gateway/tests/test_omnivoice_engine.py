"""Tests unitarios para el motor OmniVoice."""

from __future__ import annotations

import pytest

from tts_api.core.exceptions import (
    UnsupportedInstructError,
    VoiceNotFoundError,
)
from tts_api.core.omnivoice_engine import (
    STOCK_VOICE_INSTRUCTS,
    SUPPORTED_EMOTIONS,
    VALID_INSTRUCT_TOKENS_EN,
    GenerationParams,
    OmniVoiceEngine,
    _apply_emotion,
    _map_language,
    _validate_instruct,
    close_engine,
    get_engine,
)


@pytest.fixture(autouse=True)
def reset_engine():
    """Resetea el singleton del engine entre tests."""
    OmniVoiceEngine._instance = None
    OmniVoiceEngine._initialized = False
    import tts_api.core.omnivoice_engine as mod

    mod._engine_instance = None
    yield
    OmniVoiceEngine._instance = None
    OmniVoiceEngine._initialized = False
    mod._engine_instance = None


@pytest.fixture
def engine() -> OmniVoiceEngine:
    """Engine para tests (ya inicializado)."""
    e = OmniVoiceEngine()
    e._model = object()
    e._stock_voices = e._get_mock_stock_voices()
    return e


@pytest.mark.asyncio
async def test_engine_singleton():
    e1 = OmniVoiceEngine()
    e2 = OmniVoiceEngine()
    assert e1 is e2


@pytest.mark.asyncio
async def test_engine_initialize(engine: OmniVoiceEngine) -> None:
    assert engine._model is not None
    assert len(engine._stock_voices) > 0


@pytest.mark.asyncio
async def test_engine_warmup(engine: OmniVoiceEngine) -> None:
    await engine.warmup()


@pytest.mark.asyncio
async def test_engine_synthesize_stock_success(engine: OmniVoiceEngine) -> None:
    wav = await engine.synthesize_stock(
        text="Hola",
        voice_id="es-mx-male",
    )
    assert isinstance(wav, bytes)
    assert len(wav) > 0


@pytest.mark.asyncio
async def test_engine_synthesize_stock_voice_not_found(engine: OmniVoiceEngine) -> None:
    with pytest.raises(VoiceNotFoundError):
        await engine.synthesize_stock(text="Hola", voice_id="nonexistent")


@pytest.mark.asyncio
async def test_engine_synthesize_stock_with_generation_params(engine: OmniVoiceEngine) -> None:
    params = GenerationParams(num_step=16, denoise=False, guidance_scale=3.0)
    wav = await engine.synthesize_stock(
        text="Hola",
        voice_id="es-mx-male",
        generation_params=params,
    )
    assert isinstance(wav, bytes)


@pytest.mark.asyncio
async def test_engine_synthesize_instruct(engine: OmniVoiceEngine) -> None:
    wav = await engine.synthesize_instruct(
        text="Hello",
        instruct="female, young adult, british accent",
    )
    assert isinstance(wav, bytes)


@pytest.mark.asyncio
async def test_engine_synthesize_instruct_invalid(engine: OmniVoiceEngine) -> None:
    with pytest.raises(UnsupportedInstructError):
        await engine.synthesize_instruct(
            text="Hello",
            instruct="Mexican Spanish accent",
        )


@pytest.mark.asyncio
async def test_engine_synthesize_clone(engine: OmniVoiceEngine) -> None:
    wav = await engine.synthesize_clone(
        text="Hola",
        reference_audio_path="/tmp/ref.wav",
    )
    assert isinstance(wav, bytes)


@pytest.mark.asyncio
async def test_engine_synthesize_clone_with_instruct(engine: OmniVoiceEngine) -> None:
    wav = await engine.synthesize_clone(
        text="Hola",
        reference_audio_path="/tmp/ref.wav",
        instruct="male, portuguese accent",
    )
    assert isinstance(wav, bytes)


@pytest.mark.asyncio
async def test_engine_list_stock_voices(engine: OmniVoiceEngine) -> None:
    voices = await engine.list_stock_voices()
    assert len(voices) == 22


@pytest.mark.asyncio
async def test_engine_list_stock_voices_filtered(engine: OmniVoiceEngine) -> None:
    voices = await engine.list_stock_voices(language="es")
    assert all(v["language"] == "es" for v in voices)


@pytest.mark.asyncio
async def test_engine_health_check(engine: OmniVoiceEngine) -> None:
    health = await engine.health_check()
    assert health["model_loaded"] is True
    assert "gpu_available" in health


@pytest.mark.asyncio
async def test_engine_generate_mock_wav(engine: OmniVoiceEngine) -> None:
    wav = engine._generate_test_tone_wav(duration_sec=1.0, sample_rate=22050)
    assert isinstance(wav, bytes)
    assert len(wav) > 44


# --- Validación de instructs ---


def test_validate_instruct_valid():
    _validate_instruct("male, british accent")


def test_validate_instruct_single_token():
    _validate_instruct("female")


def test_validate_instruct_invalid():
    with pytest.raises(UnsupportedInstructError):
        _validate_instruct("Mexican Spanish accent")


def test_validate_instruct_mixed_valid_invalid():
    with pytest.raises(UnsupportedInstructError):
        _validate_instruct("male, Mexican Spanish accent")


def test_all_stock_voice_instructs_are_valid():
    """Todos los instructs de STOCK_VOICE_INSTRUCTS deben contener solo tokens válidos."""
    for _voice_id, instruct in STOCK_VOICE_INSTRUCTS.items():
        _validate_instruct(instruct)  # Should not raise


def test_valid_tokens_complete():
    assert "male" in VALID_INSTRUCT_TOKENS_EN
    assert "female" in VALID_INSTRUCT_TOKENS_EN
    assert "child" in VALID_INSTRUCT_TOKENS_EN
    assert "elderly" in VALID_INSTRUCT_TOKENS_EN
    assert "british accent" in VALID_INSTRUCT_TOKENS_EN
    assert "whisper" in VALID_INSTRUCT_TOKENS_EN


# --- GenerationParams ---


def test_generation_params_defaults():
    p = GenerationParams()
    assert p.num_step == 32
    assert p.denoise is True
    assert p.guidance_scale == 2.0
    assert p.duration is None


def test_generation_params_to_kwargs():
    p = GenerationParams(num_step=16, duration=5.0)
    kw = p.to_kwargs()
    assert kw["num_step"] == 16
    assert kw["duration"] == 5.0
    assert kw["denoise"] is True


@pytest.mark.asyncio
async def test_get_engine_singleton() -> None:
    e1 = await get_engine()
    e2 = await get_engine()
    assert e1 is e2


@pytest.mark.asyncio
async def test_close_engine() -> None:
    await get_engine()
    await close_engine()
    import tts_api.core.omnivoice_engine as mod

    assert mod._engine_instance is None


@pytest.mark.asyncio
async def test_close_engine_when_none() -> None:
    await close_engine()


# --- Emotion tag tests ---


def test_apply_emotion_basic():
    assert _apply_emotion("Hello!", "happy") == "[happy] Hello!"


def test_apply_emotion_case_insensitive():
    assert _apply_emotion("Hello!", "HAPPY") == "[happy] Hello!"


def test_apply_emotion_dedup():
    assert _apply_emotion("[happy] Hello!", "happy") == "[happy] Hello!"


def test_apply_emotion_none():
    assert _apply_emotion("Hello!", None) == "Hello!"


def test_apply_emotion_unsupported():
    assert _apply_emotion("Hello!", "bored") == "Hello!"


def test_apply_emotion_all_tags():
    for emotion in SUPPORTED_EMOTIONS:
        result = _apply_emotion("Test", emotion)
        assert result.startswith("["), f"Emotion {emotion} should prepend a tag"


def test_apply_emotion_singing():
    assert _apply_emotion("Twinkle star", "singing") == "[singing] Twinkle star"


# --- Language mapping tests ---


def test_map_language_known():
    assert _map_language("es") == "Spanish"
    assert _map_language("en") == "English"
    assert _map_language("zh") == "Chinese"
    assert _map_language("ja") == "Japanese"


def test_map_language_none():
    assert _map_language(None) is None


def test_map_language_unknown():
    assert _map_language("xx") == "xx"


# --- Engine synthesize with emotion ---


@pytest.mark.asyncio
async def test_engine_synthesize_stock_with_emotion(engine: OmniVoiceEngine) -> None:
    wav = await engine.synthesize_stock(
        text="Hello!",
        voice_id="en-us-male",
        emotion="happy",
    )
    assert isinstance(wav, bytes)
    assert len(wav) > 0


@pytest.mark.asyncio
async def test_engine_synthesize_stock_with_emotion_and_language(engine: OmniVoiceEngine) -> None:
    wav = await engine.synthesize_stock(
        text="Hola mundo",
        voice_id="es-mx-male",
        emotion="sad",
        language="es",
    )
    assert isinstance(wav, bytes)


@pytest.mark.asyncio
async def test_engine_synthesize_instruct_with_emotion(engine: OmniVoiceEngine) -> None:
    wav = await engine.synthesize_instruct(
        text="Welcome!",
        instruct="female, british accent",
        emotion="excited",
        language="en",
    )
    assert isinstance(wav, bytes)


@pytest.mark.asyncio
async def test_engine_synthesize_clone_with_emotion(engine: OmniVoiceEngine) -> None:
    wav = await engine.synthesize_clone(
        text="Hello!",
        reference_audio_path="/tmp/ref.wav",
        emotion="angry",
        language="en",
    )
    assert isinstance(wav, bytes)


# --- Spanish accent validation ---


def test_spanish_accent_in_valid_tokens():
    assert "spanish accent" in VALID_INSTRUCT_TOKENS_EN


def test_spanish_voice_instructs_use_spanish_accent():
    for voice_id in ["es-mx-male", "es-mx-female", "es-es-male", "es-es-female"]:
        assert "spanish accent" in STOCK_VOICE_INSTRUCTS[voice_id]
