"""Tests for the Fish Audio cloud engine."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import msgpack
import pytest

from tts_api.core.engines.fish_tts_engine import FishTTSEngine
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

    monkeypatch.setattr("tts_api.core.engines.fish_tts_engine.httpx.AsyncClient", factory)


def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FISH_API_KEY", "fish-key")
    monkeypatch.setenv("FISH_MODEL", "s2.1-pro-free")
    monkeypatch.setenv(
        "FISH_VOICES",
        json.dumps(
            {
                "es_fish": {"reference_id": "ref-es", "language": "es", "gender": "female"},
                "en_fish": "ref-en",
            }
        ),
    )


async def test_initialize_without_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FISH_API_KEY", "")
    with pytest.raises(EngineUnavailableError, match="FISH_API_KEY"):
        await FishTTSEngine().initialize()


async def test_capabilities_and_stock_voices(monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch)
    engine = FishTTSEngine()

    caps = engine.capabilities
    assert engine.name == "fish_tts"
    assert caps.locations == ["cloud"]
    assert caps.voice_cloning is True
    assert caps.instruct_voice_design is False

    voices = await engine.list_stock_voices()
    assert {v["voice_id"] for v in voices} == {"es_fish", "en_fish"}
    assert all(v["engine"] == "fish_tts" for v in voices)
    assert [v["voice_id"] for v in await engine.list_stock_voices("es")] == ["es_fish"]


async def test_synthesize_posts_json_with_reference_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers["authorization"]
        captured["model"] = request.headers["model"]
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, content=WAV_BYTES)

    _patch_client(monkeypatch, handler)
    engine = FishTTSEngine()
    await engine.initialize()

    result = await engine.synthesize(text="hola", voice_id="es_fish", language="es", speed=1.2)

    assert result == WAV_BYTES
    assert captured["path"] == "/v1/tts"
    assert captured["auth"] == "Bearer fish-key"
    assert captured["model"] == "s2.1-pro-free"
    assert captured["json"] == {
        "text": "hola",
        "format": "wav",
        "sample_rate": 24000,
        "language": "es",
        "speed": 1.2,
        "reference_id": "ref-es",
    }
    await engine.close()


async def test_unknown_voice_is_sent_as_reference_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, content=WAV_BYTES)

    _patch_client(monkeypatch, handler)
    engine = FishTTSEngine()
    await engine.initialize()
    await engine.synthesize(text="hi", voice_id="fish-library-id", language="en")

    assert captured["json"]["reference_id"] == "fish-library-id"
    await engine.close()


async def test_clone_uses_messagepack_references(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _configured(monkeypatch)
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"raw-audio")
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content_type"] = request.headers["content-type"]
        captured["payload"] = msgpack.unpackb(request.content, raw=False)
        return httpx.Response(200, content=WAV_BYTES)

    _patch_client(monkeypatch, handler)
    engine = FishTTSEngine()
    await engine.initialize()

    result = await engine.synthesize_clone(
        text="hola",
        reference_audio_path=str(reference),
        language="es",
        ref_text="transcripción",
    )

    assert result == WAV_BYTES
    assert captured["content_type"] == "application/msgpack"
    assert captured["payload"]["references"] == [{"audio": b"raw-audio", "text": "transcripción"}]
    await engine.close()


async def test_instruct_is_not_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch)
    with pytest.raises(FeatureNotSupportedError):
        await FishTTSEngine().synthesize_instruct(text="hola", instruct="voz grave", language="es")


async def test_http_error_becomes_engine_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key")

    _patch_client(monkeypatch, handler)
    engine = FishTTSEngine()
    await engine.initialize()
    with pytest.raises(EngineUnavailableError, match="401"):
        await engine.synthesize(text="hola", voice_id="es_fish", language="es")
    await engine.close()
