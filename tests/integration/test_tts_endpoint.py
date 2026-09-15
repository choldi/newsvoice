"""Integration tests for POST /generate_tts endpoint."""

import io
import json
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.core.config import settings

client = TestClient(app)


def _fake_wav(rate: int = 22050) -> bytes:
    """Return a valid 1-second silent WAV payload for tests."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * rate)
    return buffer.getvalue()


@pytest.fixture
def fake_omni_voice(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Patch the httpx client used by TTSService to return a fake WAV."""

    class FakeResponse:
        def __init__(self, content: bytes | None = None) -> None:
            self.content = content or b""

        def raise_for_status(self) -> None:
            pass

        def json(self) -> list | dict:
            return json.loads(self.content)

    class FakeAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def get(self, url: str, headers: dict | None = None) -> FakeResponse:
            if url.endswith("/voices/stock"):
                payload = b'[{"voice_id": "es-mx-male", "language": "es", "gender": "male"}]'
                return FakeResponse(payload)
            return FakeResponse(b"[]")

        async def post(self, *args: object, **kwargs: object) -> FakeResponse:
            return FakeResponse(_fake_wav())

    monkeypatch.setattr("src.api.services.tts.httpx.AsyncClient", FakeAsyncClient)
    settings.tts_cache_dir = str(tmp_path)


def test_tts_endpoint_success(fake_omni_voice: None) -> None:
    """TTS endpoint returns audio_path JSON on success."""
    payload = {
        "markdown": "# Historias principales\n1. **A** — resumen",
        "voice": "es-mx-male",
        "language": "es",
    }
    response = client.post("/api/generate_tts", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["audio_path"].endswith(".wav")
    assert body["cached"] is False
    assert body["voice"] == "es-mx-male"
    assert body["char_count"] > 0


def test_tts_endpoint_caches_second_call(fake_omni_voice: None) -> None:
    """Second call with the same payload returns cached=True."""
    payload = {"markdown": "contenido cacheable", "voice": "es-mx-male", "language": "es"}
    first = client.post("/api/generate_tts", json=payload).json()
    second = client.post("/api/generate_tts", json=payload).json()
    assert first["cached"] is False
    assert second["cached"] is True
    assert first["audio_path"] == second["audio_path"]


def test_tts_endpoint_empty_markdown() -> None:
    """Empty markdown returns 422."""
    response = client.post("/api/generate_tts", json={"markdown": ""})
    assert response.status_code == 422
