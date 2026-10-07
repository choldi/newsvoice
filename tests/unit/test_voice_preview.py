"""Unit tests for the voice preview service (Phase 9.4)."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.api.models.narration import TTSBackend
from src.api.models.voices import VoiceProfile
from src.api.services import voice_preview
from src.core.config import settings
from src.core.voice_catalog import save_custom_profile

REF_TEXT = "Hola, soy tu profe de ingles de siempre. " * 4
INSTRUCT = "Femenina, 30s, tono cercano y agil, claridad de locucion"


@pytest.fixture(autouse=True)
def _tmp_voices_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate samples/manifests and start from Modal off."""
    voices = tmp_path / "voices"
    voices.mkdir()
    monkeypatch.setattr(settings, "voices_dir", str(voices))
    monkeypatch.setattr(settings, "modal_enabled", False)
    return voices


def _profile(name: str = "mi_voz_es", language: str = "es") -> VoiceProfile:
    """Build a valid custom profile for the fallback tests."""
    return VoiceProfile.model_validate(
        {
            "name": name,
            "label": name,
            "language": language,
            "instruct": INSTRUCT,
            "ref_text": REF_TEXT,
        }
    )


def _write_reference(name: str, voices: Path) -> Path:
    """Drop a fake reference WAV for a profile."""
    wav = voices / "refs" / f"{name}.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"RIFF-REFERENCE")
    return wav


# --- preview ------------------------------------------------------------------


async def test_preview_synthesizes_then_serves_from_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice_for = AsyncMock(return_value="es-mx-male")
    synthesize = AsyncMock(return_value=b"AUDIO-BYTES")
    monkeypatch.setattr(voice_preview, "voice_for", voice_for)
    monkeypatch.setattr(voice_preview, "synthesize", synthesize)

    first = await voice_preview.preview("es-mx-male")
    second = await voice_preview.preview("es-mx-male")

    assert first["engine"] == "omni_voice"
    assert first["note"] == "muestra sintetizada"
    assert second["note"] == "muestra en caché"
    assert first["url"] == second["url"]
    assert first["url"].startswith("/ui/voices/sample/es-mx-male-omni_voice-")
    assert first["url"].endswith(".wav")
    filename = first["url"].rsplit("/", 1)[-1]
    assert (voice_preview.samples_dir() / filename).read_bytes() == b"AUDIO-BYTES"
    voice_for.assert_awaited_once()
    synthesize.assert_awaited_once()


async def test_preview_prefers_edge_for_an_edge_shortname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice_for = AsyncMock(return_value="es-ES-DalvaNeural")
    synthesize = AsyncMock(return_value=b"MP3-BYTES")
    monkeypatch.setattr(voice_preview, "voice_for", voice_for)
    monkeypatch.setattr(voice_preview, "synthesize", synthesize)

    result = await voice_preview.preview("es-ES-DalvaNeural")

    assert result["engine"] == "edge"
    assert result["url"].endswith(".mp3")
    assert voice_for.await_args.args[0] is TTSBackend.EDGE


async def test_preview_falls_back_to_the_reference_when_modal_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_custom_profile(_profile())
    _write_reference("mi_voz_es", Path(settings.voices_dir))
    voice_for = AsyncMock(side_effect=AssertionError("must not resolve"))
    synthesize = AsyncMock(side_effect=AssertionError("must not synthesize"))
    monkeypatch.setattr(voice_preview, "voice_for", voice_for)
    monkeypatch.setattr(voice_preview, "synthesize", synthesize)

    result = await voice_preview.preview("mi_voz_es")

    assert result["url"] == "/ui/voices/audio/mi_voz_es.wav"
    assert result["engine"] == "qwen3_tts"
    assert "modal apagado" in result["note"]
    voice_for.assert_not_awaited()
    synthesize.assert_not_awaited()
    assert not voice_preview.samples_dir().exists()


async def test_preview_falls_back_to_the_reference_when_the_synthesis_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "modal_enabled", True)
    save_custom_profile(_profile())
    _write_reference("mi_voz_es", Path(settings.voices_dir))
    voice_for = AsyncMock(return_value="mi_voz_es")
    synthesize = AsyncMock(side_effect=RuntimeError("modal caído"))
    monkeypatch.setattr(voice_preview, "voice_for", voice_for)
    monkeypatch.setattr(voice_preview, "synthesize", synthesize)

    result = await voice_preview.preview("mi_voz_es")

    assert result["url"] == "/ui/voices/audio/mi_voz_es.wav"
    assert "fallo de síntesis" in result["note"]


async def test_preview_reports_a_missing_reference_when_modal_is_off() -> None:
    save_custom_profile(_profile())

    with pytest.raises(voice_preview.VoicePreviewError, match="modal apagado"):
        await voice_preview.preview("mi_voz_es")


async def test_preview_raises_when_every_engine_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice_for = AsyncMock(return_value="es-mx-male")
    synthesize = AsyncMock(side_effect=RuntimeError("gateway caído"))
    monkeypatch.setattr(voice_preview, "voice_for", voice_for)
    monkeypatch.setattr(voice_preview, "synthesize", synthesize)

    with pytest.raises(voice_preview.VoicePreviewError, match="gateway caído"):
        await voice_preview.preview("es-mx-male")


async def test_preview_rejects_a_voice_unknown_to_every_engine() -> None:
    with pytest.raises(voice_preview.VoicePreviewError, match="desconocida"):
        await voice_preview.preview("vox_999")


async def test_preview_requires_a_voice() -> None:
    with pytest.raises(voice_preview.VoicePreviewError, match="selecciona"):
        await voice_preview.preview("   ")


async def test_preview_uses_the_profile_language_for_the_phrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "modal_enabled", True)
    save_custom_profile(_profile("mi_voz_en", language="en"))
    voice_for = AsyncMock(return_value="mi_voz_en")
    synthesize = AsyncMock(return_value=b"EN-AUDIO")
    monkeypatch.setattr(voice_preview, "voice_for", voice_for)
    monkeypatch.setattr(voice_preview, "synthesize", synthesize)

    result = await voice_preview.preview("mi_voz_en")

    assert result["engine"] == "qwen3_tts"
    assert synthesize.await_args.args[1] == voice_preview._SAMPLE_TEXTS["en"]


# --- helpers ------------------------------------------------------------------


def test_cached_sample_refuses_unsafe_names(tmp_path: Path) -> None:
    assert voice_preview.cached_sample("../evil.wav") is None
    assert voice_preview.cached_sample("x.exe") is None
    assert voice_preview.cached_sample("not-there-omni_voice-0123456789ab.wav") is None

    samples = voice_preview.samples_dir()
    samples.mkdir(parents=True, exist_ok=True)
    good = "es-mx-male-omni_voice-0123456789ab.wav"
    (samples / good).write_bytes(b"A")

    path = voice_preview.cached_sample(good)
    assert path is not None
    assert path.read_bytes() == b"A"


def test_sample_filename_is_deterministic_and_safe() -> None:
    args = ("es-ES-DalvaNeural", TTSBackend.EDGE, "es", "hola")

    name = voice_preview.sample_filename(*args)

    assert name == voice_preview.sample_filename(*args)
    assert name.startswith("es-es-dalvaneural-edge-")
    assert name.endswith(".mp3")

    weird = voice_preview.sample_filename("Mi Voz/Extraña!", TTSBackend.OMNIVOICE, "es", "hola")
    assert re.fullmatch(r"[a-z0-9_-]+\.(?:mp3|wav)", weird)


def test_media_type_for_samples() -> None:
    assert voice_preview.media_type_for("x.mp3") == "audio/mpeg"
    assert voice_preview.media_type_for("x.wav") == "audio/wav"
    assert voice_preview.media_type_for("x.bin") == "application/octet-stream"


def test_language_hint_prefers_the_catalog_then_the_id_prefix() -> None:
    save_custom_profile(_profile("mi_voz_en", language="en"))

    assert voice_preview._language_hint("mi_voz_en") == "en"
    assert voice_preview._language_hint("es-ES-DalvaNeural") == "es"
    assert voice_preview._language_hint("es-mx-male") == "es"
    assert (
        voice_preview._language_hint("f47ac10b-56cc-4720-a6c1-111111111111")
        == settings.tts_language
    )
