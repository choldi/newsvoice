"""Integration tests for the TTS endpoints (admin + removed public alias)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import src.api.services.tts as tts_module
from src.api.main import app
from src.core.config import settings

client = TestClient(app)

_FAKE_MP3 = b"\xff\xfb\x90\x64" * 64
_TOKEN = "test-token"
AUTH_HEADERS = {"X-Admin-Token": _TOKEN}


class _FakeCommunicate:
    """Fake edge_tts.Communicate returning MP3-ish audio bytes."""

    def __init__(self, text: str, voice: str, **kwargs: object) -> None:
        self.text = text
        self.voice = voice

    async def stream(self):
        yield {"type": "audio", "data": _FAKE_MP3}


@pytest.fixture(autouse=True)
def _admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the admin token used by the endpoint tests."""
    monkeypatch.setattr(settings, "admin_api_token", _TOKEN)


@pytest.fixture
def fake_edge_tts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Patch edge-tts so synthesis is deterministic and offline."""
    monkeypatch.setattr(tts_module.edge_tts, "Communicate", _FakeCommunicate)
    monkeypatch.setattr(tts_module, "_EDGE_VOICES", [{"ShortName": "es-MX-JorgeNeural"}])
    settings.tts_cache_dir = str(tmp_path)


def test_tts_endpoint_success(fake_edge_tts: None) -> None:
    """The admin TTS endpoint returns audio_path JSON on success."""
    payload = {
        "markdown": "# Historias principales\n1. **A** — resumen",
        "voice": "es-MX-JorgeNeural",
        "language": "es",
    }
    response = client.post("/api/admin/tts", json=payload, headers=AUTH_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["audio_path"].endswith(".mp3")
    assert body["cached"] is False
    assert body["voice"] == "es-MX-JorgeNeural"
    assert body["char_count"] > 0


def test_tts_endpoint_requires_admin_token(fake_edge_tts: None) -> None:
    """Without the admin token the synthesis endpoint returns 401."""
    response = client.post("/api/admin/tts", json={"markdown": "hola"})
    assert response.status_code == 401
    assert response.json()["status"] == "error"


def test_tts_endpoint_rejects_wrong_token(fake_edge_tts: None) -> None:
    """A wrong admin token is rejected with 401."""
    response = client.post(
        "/api/admin/tts", json={"markdown": "hola"}, headers={"X-Admin-Token": "nope"}
    )
    assert response.status_code == 401


def test_tts_endpoint_kill_switch(fake_edge_tts: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """TTS_API_ENABLED=false closes the endpoint with 503."""
    monkeypatch.setattr(settings, "tts_api_enabled", False)
    response = client.post("/api/admin/tts", json={"markdown": "hola"}, headers=AUTH_HEADERS)
    assert response.status_code == 503
    assert "TTS_API_ENABLED" in response.json()["detail"]


def test_tts_endpoint_caches_second_call(fake_edge_tts: None) -> None:
    """Second call with the same payload returns cached=True."""
    payload = {"markdown": "contenido cacheable", "voice": "es-MX-JorgeNeural", "language": "es"}
    first = client.post("/api/admin/tts", json=payload, headers=AUTH_HEADERS).json()
    second = client.post("/api/admin/tts", json=payload, headers=AUTH_HEADERS).json()
    assert first["cached"] is False
    assert second["cached"] is True
    assert first["audio_path"] == second["audio_path"]


def test_tts_endpoint_regenerates_when_content_changes(fake_edge_tts: None) -> None:
    """A different digest overwrites the cached audio instead of reusing it."""
    base = {"voice": "es-MX-JorgeNeural", "language": "es"}
    first = client.post(
        "/api/admin/tts", json={**base, "markdown": "primera"}, headers=AUTH_HEADERS
    )
    second = client.post(
        "/api/admin/tts", json={**base, "markdown": "segunda versión"}, headers=AUTH_HEADERS
    )
    assert first.status_code == 200
    assert second.json()["cached"] is False
    assert second.json()["audio_path"] == first.json()["audio_path"]


def test_tts_endpoint_legacy_voice_alias(fake_edge_tts: None) -> None:
    """A legacy OmniVoice voice name is accepted and normalized."""
    payload = {"markdown": "hola", "voice": "es-mx-male", "language": "es"}
    response = client.post("/api/admin/tts", json=payload, headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert response.json()["audio_path"].endswith(".mp3")


def test_tts_endpoint_empty_markdown() -> None:
    """Empty markdown returns 422."""
    response = client.post("/api/admin/tts", json={"markdown": ""}, headers=AUTH_HEADERS)
    assert response.status_code == 422


def test_tts_endpoint_oversized_markdown_rejected(fake_edge_tts: None) -> None:
    """A payload far beyond the synthesis limit is rejected with 422."""
    response = client.post("/api/admin/tts", json={"markdown": "a" * 25_000}, headers=AUTH_HEADERS)
    assert response.status_code == 422


def test_public_generate_tts_is_gone() -> None:
    """The former public endpoint answers 410 Gone without synthesizing."""
    response = client.post("/api/generate_tts", json={"markdown": "hola"})
    assert response.status_code == 410
    assert "/api/admin/tts" in response.json()["detail"]
