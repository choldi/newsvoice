"""Integration tests for POST /generate_tts endpoint."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.core.config import settings

client = TestClient(app)


@pytest.fixture
def fake_edge_tts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Patch edge-tts to write a fake MP3 instead of calling the network."""

    class FakeCommunicate:
        def __init__(self, text: str, voice: str) -> None:
            self.text = text
            self.voice = voice

        async def save(self, path: str) -> None:
            Path(path).write_bytes(b"fake-mp3-bytes")

    monkeypatch.setattr("src.api.services.tts.edge_tts.Communicate", FakeCommunicate)
    settings.tts_cache_dir = str(tmp_path)


def test_tts_endpoint_success(fake_edge_tts: None) -> None:
    """TTS endpoint returns audio_path JSON on success."""
    payload = {
        "markdown": "# Historias principales\n1. **A** — resumen",
        "voice": "es-ES-ElviraNeural",
    }
    response = client.post("/api/generate_tts", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["audio_path"].endswith(".mp3")
    assert body["cached"] is False
    assert body["voice"] == "es-ES-ElviraNeural"
    assert body["char_count"] > 0


def test_tts_endpoint_caches_second_call(fake_edge_tts: None) -> None:
    """Second call with the same payload returns cached=True."""
    payload = {"markdown": "contenido cacheable", "voice": "es-ES-ElviraNeural"}
    first = client.post("/api/generate_tts", json=payload).json()
    second = client.post("/api/generate_tts", json=payload).json()
    assert first["cached"] is False
    assert second["cached"] is True
    assert first["audio_path"] == second["audio_path"]


def test_tts_endpoint_empty_markdown() -> None:
    """Empty markdown returns 422."""
    response = client.post("/api/generate_tts", json={"markdown": ""})
    assert response.status_code == 422
