"""Integration tests for POST /generate_tts endpoint."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import src.api.services.tts as tts_module
from src.api.main import app
from src.core.config import settings

client = TestClient(app)

_FAKE_MP3 = b"\xff\xfb\x90\x64" * 64


class _FakeCommunicate:
    """Fake edge_tts.Communicate returning MP3-ish audio bytes."""

    def __init__(self, text: str, voice: str, **kwargs: object) -> None:
        self.text = text
        self.voice = voice

    async def stream(self):
        yield {"type": "audio", "data": _FAKE_MP3}


@pytest.fixture
def fake_edge_tts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Patch edge-tts so synthesis is deterministic and offline."""
    monkeypatch.setattr(tts_module.edge_tts, "Communicate", _FakeCommunicate)
    monkeypatch.setattr(tts_module, "_EDGE_VOICES", [{"ShortName": "es-MX-JorgeNeural"}])
    settings.tts_cache_dir = str(tmp_path)


def test_tts_endpoint_success(fake_edge_tts: None) -> None:
    """TTS endpoint returns audio_path JSON on success."""
    payload = {
        "markdown": "# Historias principales\n1. **A** — resumen",
        "voice": "es-MX-JorgeNeural",
        "language": "es",
    }
    response = client.post("/api/generate_tts", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["audio_path"].endswith(".mp3")
    assert body["cached"] is False
    assert body["voice"] == "es-MX-JorgeNeural"
    assert body["char_count"] > 0


def test_tts_endpoint_caches_second_call(fake_edge_tts: None) -> None:
    """Second call with the same payload returns cached=True."""
    payload = {"markdown": "contenido cacheable", "voice": "es-MX-JorgeNeural", "language": "es"}
    first = client.post("/api/generate_tts", json=payload).json()
    second = client.post("/api/generate_tts", json=payload).json()
    assert first["cached"] is False
    assert second["cached"] is True
    assert first["audio_path"] == second["audio_path"]


def test_tts_endpoint_legacy_voice_alias(fake_edge_tts: None) -> None:
    """A legacy OmniVoice voice name is accepted and normalized."""
    payload = {"markdown": "hola", "voice": "es-mx-male", "language": "es"}
    response = client.post("/api/generate_tts", json=payload)
    assert response.status_code == 200
    assert response.json()["audio_path"].endswith(".mp3")


def test_tts_endpoint_empty_markdown() -> None:
    """Empty markdown returns 422."""
    response = client.post("/api/generate_tts", json={"markdown": ""})
    assert response.status_code == 422
