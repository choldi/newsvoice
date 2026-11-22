"""Tests for the declarative OpenAI-compatible engine adapter."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tts_api.core.engine_registry import EngineCapabilitySpec, EngineSpec
from tts_api.core.engines.openai_compatible_engine import OpenAICompatibleEngine
from tts_api.core.exceptions import (
    EngineUnavailableError,
    FeatureNotSupportedError,
    UnsupportedLanguageError,
)

_WAV = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00"


def _spec() -> EngineSpec:
    return EngineSpec(
        name="vendor",
        kind="openai_compatible_http",
        display_name="Vendor TTS",
        locations=["cloud"],
        capabilities=EngineCapabilitySpec(supported_languages=["es", "en"]),
        options={
            "base_url_env": "VENDOR_BASE_URL",
            "api_key_env": "VENDOR_API_KEY",
            "model_env": "VENDOR_MODEL",
            "languages": ["es", "en"],
            "voices": {"vendor-es": {"voice": "es-ES-Voice", "language": "es"}},
        },
    )


async def test_initialize_uses_declared_environment() -> None:
    engine = OpenAICompatibleEngine(_spec())

    with pytest.raises(EngineUnavailableError, match="VENDOR_BASE_URL"):
        await engine.initialize()


async def test_synthesize_posts_the_declared_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VENDOR_BASE_URL", "https://vendor.example/v1/")
    monkeypatch.setenv("VENDOR_API_KEY", "secret")
    monkeypatch.setenv("VENDOR_MODEL", "vendor-2")
    engine = OpenAICompatibleEngine(_spec())
    await engine.initialize()
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert request.url.path == "/v1/audio/speech"
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(200, content=_WAV)

    await engine._client.aclose()
    engine._client = httpx.AsyncClient(
        base_url="https://vendor.example/v1", transport=httpx.MockTransport(handler)
    )

    audio = await engine.synthesize(text="Hola", voice_id="vendor-es", language="es", speed=1.5)

    assert audio == _WAV
    assert seen == [
        {
            "model": "vendor-2",
            "input": "Hola",
            "voice": "es-ES-Voice",
            "response_format": "wav",
            "speed": 1.5,
        }
    ]
    await engine.close()


async def test_synthesize_rejects_undeclared_language() -> None:
    engine = OpenAICompatibleEngine(_spec())
    engine._available = True

    with pytest.raises(UnsupportedLanguageError):
        await engine.synthesize(text="Hola", voice_id="vendor-es", language="de")


async def test_clone_and_instruct_are_unsupported() -> None:
    engine = OpenAICompatibleEngine(_spec())

    with pytest.raises(FeatureNotSupportedError):
        await engine.synthesize_clone(text="Hola", reference_audio_path="ref.wav", language="es")
    with pytest.raises(FeatureNotSupportedError):
        await engine.synthesize_instruct(text="Hola", instruct="bright", language="es")


async def test_stock_voices_use_declared_metadata() -> None:
    engine = OpenAICompatibleEngine(_spec())

    voices = await engine.list_stock_voices("es")

    assert voices == [
        {
            "voice_id": "vendor-es",
            "language": "es",
            "gender": "",
            "name": "vendor-es",
            "engine": "vendor",
        }
    ]
