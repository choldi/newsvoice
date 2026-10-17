"""Integration tests for the voice preview picker (Phase 9.4)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.models.voices import VoiceProfile
from src.core.config import settings
from src.core.voice_catalog import load_profiles, save_custom_profile

TOKEN = "secret-admin-token"
REF_TEXT = "Hola, soy tu profe de ingles de siempre. " * 4
INSTRUCT = "Femenina, 30s, tono cercano y agil, claridad de locucion"


@pytest.fixture(autouse=True)
def _voices_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the voices directory and auth, and keep the gateway out."""
    voices = tmp_path / "voices"
    voices.mkdir()
    monkeypatch.setattr(settings, "voices_dir", str(voices))
    monkeypatch.setattr(settings, "admin_api_token", TOKEN)
    monkeypatch.setattr(settings, "collections_file", str(tmp_path / "collections.toml"))
    monkeypatch.setattr(settings, "modal_enabled", False)
    from src.api.services.voices import VoicesService

    monkeypatch.setattr(VoicesService, "list_languages", AsyncMock(return_value=["es"]))
    monkeypatch.setattr(VoicesService, "list_stock", AsyncMock(return_value=[]))
    monkeypatch.setattr(VoicesService, "list_cloned", AsyncMock(return_value=[]))
    return voices


def _client() -> TestClient:
    """Build a client authenticated through the login endpoint."""
    client = TestClient(app)
    response = client.post("/ui/login", data={"token": TOKEN}, follow_redirects=False)
    assert response.status_code == 302
    return client


def _profile(name: str = "mi_voz_es") -> VoiceProfile:
    """Build a valid custom profile for the fallback tests."""
    return VoiceProfile.model_validate(
        {"name": name, "label": name, "language": "es", "instruct": INSTRUCT, "ref_text": REF_TEXT}
    )


def _write_reference(name: str, voices: Path) -> Path:
    """Drop a fake reference WAV for a profile."""
    wav = voices / "refs" / f"{name}.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"RIFF-REFERENCE")
    return wav


# --- page ---------------------------------------------------------------------


def test_ui_voices_page_includes_the_preview_picker() -> None:
    response = _client().get("/ui/voices")

    assert response.status_code == 200
    text = response.text
    assert 'hx-post="/ui/voices/test"' in text
    assert 'hx-target="#voice-sample"' in text
    assert 'id="voice-sample"' in text
    committed = load_profiles("voice_profiles.json")[0].name
    assert f'value="{committed}"' in text


# --- POST /ui/voices/test ------------------------------------------------------


def test_ui_test_voice_returns_an_audio_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice_for = AsyncMock(return_value="es-mx-male")
    synthesize = AsyncMock(return_value=b"AUDIO-BYTES")
    monkeypatch.setattr("src.api.services.voice_preview.voice_for", voice_for)
    monkeypatch.setattr("src.api.services.voice_preview.synthesize", synthesize)

    response = _client().post("/ui/voices/test", data={"voice": "es-mx-male"})

    assert response.status_code == 200
    text = response.text
    assert "<audio" in text
    assert "/ui/voices/sample/es-mx-male-omni_voice-" in text
    assert "muestra sintetizada" in text
    samples = list((Path(settings.voices_dir) / "samples").glob("*.wav"))
    assert len(samples) == 1
    assert samples[0].read_bytes() == b"AUDIO-BYTES"


def test_ui_test_voice_shows_a_failure_inline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice_for = AsyncMock(return_value="es-mx-male")
    synthesize = AsyncMock(side_effect=RuntimeError("gateway caído"))
    monkeypatch.setattr("src.api.services.voice_preview.voice_for", voice_for)
    monkeypatch.setattr("src.api.services.voice_preview.synthesize", synthesize)

    response = _client().post("/ui/voices/test", data={"voice": "es-mx-male"})

    assert response.status_code == 200
    assert "gateway caído" in response.text
    assert "<audio" not in response.text


def test_ui_test_voice_rejects_an_unknown_voice() -> None:
    response = _client().post("/ui/voices/test", data={"voice": "vox_999"})

    assert response.status_code == 200
    assert "desconocida" in response.text
    assert "<audio" not in response.text


def test_ui_test_voice_requires_a_voice() -> None:
    response = _client().post("/ui/voices/test", data={})

    assert response.status_code == 200
    assert "selecciona una voz" in response.text


def test_ui_test_voice_falls_back_to_the_reference_when_modal_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_custom_profile(_profile())
    _write_reference("mi_voz_es", Path(settings.voices_dir))
    voice_for = AsyncMock(side_effect=AssertionError("must not resolve"))
    synthesize = AsyncMock(side_effect=AssertionError("must not synthesize"))
    monkeypatch.setattr("src.api.services.voice_preview.voice_for", voice_for)
    monkeypatch.setattr("src.api.services.voice_preview.synthesize", synthesize)

    response = _client().post("/ui/voices/test", data={"voice": "mi_voz_es"})

    assert response.status_code == 200
    text = response.text
    assert "/ui/voices/audio/mi_voz_es.wav" in text
    assert "modal apagado" in text
    voice_for.assert_not_awaited()
    synthesize.assert_not_awaited()


# --- GET /ui/voices/sample/{filename} ------------------------------------------


def test_sample_endpoint_serves_cached_audio() -> None:
    samples = Path(settings.voices_dir) / "samples"
    samples.mkdir(parents=True, exist_ok=True)
    wav = "es-mx-male-omni_voice-0123456789ab.wav"
    mp3 = "es-es-dalvaneural-edge-0123456789ab.mp3"
    (samples / wav).write_bytes(b"WAV-SAMPLE")
    (samples / mp3).write_bytes(b"MP3-SAMPLE")
    client = _client()

    served_wav = client.get(f"/ui/voices/sample/{wav}")
    served_mp3 = client.get(f"/ui/voices/sample/{mp3}")

    assert served_wav.status_code == 200
    assert served_wav.headers["content-type"].startswith("audio/wav")
    assert served_wav.content == b"WAV-SAMPLE"
    assert served_mp3.status_code == 200
    assert served_mp3.headers["content-type"].startswith("audio/mpeg")
    assert served_mp3.content == b"MP3-SAMPLE"


def test_sample_endpoint_rejects_unsafe_and_missing_names() -> None:
    client = _client()

    assert client.get("/ui/voices/sample/evil.exe").status_code == 404
    assert client.get("/ui/voices/sample/..%2Fsecret.wav").status_code == 404
    assert client.get("/ui/voices/sample/nope-omni_voice-0123456789ab.wav").status_code == 404
