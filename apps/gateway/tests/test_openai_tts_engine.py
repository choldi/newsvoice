"""Tests for the OpenAI-compatible speech engine."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tts_api.core.engines.openai_tts_engine import OpenAITTSEngine
from tts_api.core.exceptions import EngineUnavailableError, FeatureNotSupportedError

WAV_BYTES = b"RIFF....WAVEfmt "


def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    real_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("tts_api.core.engines.openai_tts_engine.httpx.AsyncClient", factory)


def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_TTS_BASE_URL", "https://tts.example/v1")
    monkeypatch.setenv("OPENAI_TTS_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_TTS_MODEL", "tts-1")
    monkeypatch.setenv(
        "OPENAI_TTS_VOICES",
        json.dumps({"es_openai": {"voice": "nova", "language": "es", "gender": "female"}}),
    )


async def test_initialize_without_base_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_TTS_BASE_URL", "")
    with pytest.raises(EngineUnavailableError, match="OPENAI_TTS_BASE_URL"):
        await OpenAITTSEngine().initialize()


async def test_capabilities_and_stock_voices(monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch)
    engine = OpenAITTSEngine()

    caps = engine.capabilities
    assert engine.name == "openai_tts"
    assert caps.locations == ["cloud"]
    assert caps.voice_cloning is False
    assert caps.instruct_voice_design is False

    voices = await engine.list_stock_voices()
    assert voices == [
        {
            "voice_id": "es_openai",
            "language": "es",
            "gender": "female",
            "name": "es_openai",
            "engine": "openai_tts",
        }
    ]


async def test_synthesize_posts_to_audio_speech(monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers["authorization"]
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, content=WAV_BYTES)

    _patch_client(monkeypatch, handler)
    engine = OpenAITTSEngine()
    await engine.initialize()

    result = await engine.synthesize(text="hola", voice_id="es_openai", language="es", speed=0.9)

    assert result == WAV_BYTES
    assert captured["path"] == "/v1/audio/speech"
    assert captured["auth"] == "Bearer openai-key"
    assert captured["json"] == {
        "model": "tts-1",
        "input": "hola",
        "voice": "nova",
        "response_format": "wav",
        "speed": 0.9,
    }
    await engine.close()


async def test_unknown_voice_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, content=WAV_BYTES)

    _patch_client(monkeypatch, handler)
    engine = OpenAITTSEngine()
    await engine.initialize()
    await engine.synthesize(text="hi", voice_id="alloy", language="en")

    assert captured["json"]["voice"] == "alloy"
    await engine.close()


async def test_clone_and_instruct_are_not_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch)
    engine = OpenAITTSEngine()
    with pytest.raises(FeatureNotSupportedError):
        await engine.synthesize_clone(text="hola", reference_audio_path="/tmp/x.wav", language="es")
    with pytest.raises(FeatureNotSupportedError):
        await engine.synthesize_instruct(text="hola", instruct="voz grave", language="es")
