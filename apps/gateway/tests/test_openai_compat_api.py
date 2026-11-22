"""Tests for the OpenAI-compatible speech facade."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tts_api.api.v1.tts import get_tts_service
from tts_api.core.engine_client import AudioResult
from tts_api.main import app

_WAV = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00"


class RecordingTtsService:
    """Minimal TtsService stand-in recording synthesis arguments."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def synthesize_stock(
        self, *, text: str, voice_id: str, language: str, speed: float = 1.0, **kwargs: Any
    ) -> AudioResult:
        self.calls.append(
            {
                "text": text,
                "voice_id": voice_id,
                "language": language,
                "speed": speed,
                **kwargs,
            }
        )
        return AudioResult(wav_bytes=_WAV, duration_sec=0.5, sample_rate=24000)


@pytest.fixture
def service() -> RecordingTtsService:
    return RecordingTtsService()


@pytest.fixture
def client(service: RecordingTtsService) -> Iterator[TestClient]:
    async def fake_dependency() -> AsyncIterator[RecordingTtsService]:
        yield service

    app.dependency_overrides[get_tts_service] = fake_dependency
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_tts_service, None)


def test_speech_translates_the_openai_contract(
    client: TestClient, service: RecordingTtsService
) -> None:
    response = client.post(
        "/v1/audio/speech",
        params={"language": "ES"},
        json={
            "model": "tts-1",
            "input": "Hola mundo",
            "voice": "es-mx-male",
            "response_format": "WAV",
            "speed": 1.2,
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/wav")
    assert response.content == _WAV
    assert service.calls == [
        {
            "text": "Hola mundo",
            "voice_id": "es-mx-male",
            "language": "es",
            "speed": 1.2,
        }
    ]


def test_speech_rejects_a_non_iso_language(
    client: TestClient, service: RecordingTtsService
) -> None:
    response = client.post(
        "/v1/audio/speech",
        params={"language": "es-ES"},
        json={"model": "tts-1", "input": "Hola", "voice": "es-mx-male"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error_type"] == "unsupported_language"
    assert service.calls == []


def test_speech_rejects_an_unknown_response_format(client: TestClient) -> None:
    response = client.post(
        "/v1/audio/speech",
        json={
            "model": "tts-1",
            "input": "Hola",
            "voice": "es-mx-male",
            "response_format": "ogg",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error_type"] == "unsupported_response_format"


def test_speech_rejects_an_out_of_range_speed(client: TestClient) -> None:
    response = client.post(
        "/v1/audio/speech",
        json={"model": "tts-1", "input": "Hola", "voice": "es-mx-male", "speed": 3.0},
    )

    assert response.status_code == 422
