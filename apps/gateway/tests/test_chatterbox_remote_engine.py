"""Tests for the Chatterbox HTTP proxy engine (sidecar bridge)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tts_api.core.engine_factory import create_engine
from tts_api.core.engines.chatterbox_engine import ChatterboxEngine
from tts_api.core.engines.chatterbox_remote_engine import ChatterboxRemoteEngine
from tts_api.core.exceptions import EngineUnavailableError

SIDECAR_URL = "http://chatterbox-sidecar:8000/api/v1"
WAV_BYTES = b"RIFF....WAVEfmt "


def _engine(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    base_url: str = SIDECAR_URL,
) -> ChatterboxRemoteEngine:
    engine = ChatterboxRemoteEngine(base_url=base_url)
    engine._proxy._client = httpx.AsyncClient(
        base_url=engine._base_url, transport=httpx.MockTransport(handler)
    )
    engine._proxy._available = True
    return engine


def _payload(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode())


def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    real_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("tts_api.core.engines.omnivoice_remote_engine.httpx.AsyncClient", factory)


def test_remote_engine_keeps_the_local_contract() -> None:
    engine = ChatterboxRemoteEngine(base_url=SIDECAR_URL)
    assert isinstance(engine, ChatterboxEngine)
    assert engine.name == "chatterbox"
    assert engine.capabilities == ChatterboxEngine().capabilities


def test_remote_url_comes_from_env_and_strips_the_api_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHATTERBOX_REMOTE_URL", SIDECAR_URL)
    engine = ChatterboxRemoteEngine()
    assert engine._base_url == "http://chatterbox-sidecar:8000"


async def test_initialize_without_url_stays_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHATTERBOX_REMOTE_URL", raising=False)
    engine = ChatterboxRemoteEngine()
    await engine.initialize()
    assert engine._proxy._available is False
    with pytest.raises(EngineUnavailableError):
        await engine.synthesize(text="hola", voice_id="chatterbox-default", language="es")


async def test_initialize_probes_the_sidecar_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"status": "alive"})

    _patch_client(monkeypatch, handler)
    engine = ChatterboxRemoteEngine(base_url=SIDECAR_URL)
    await engine.initialize()
    assert engine._proxy._available is True
    assert seen == ["http://chatterbox-sidecar:8000/api/v1/health/live"]
    await engine.close()


async def test_warmup_delegates_to_the_sidecar_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = ChatterboxRemoteEngine(base_url=SIDECAR_URL)
    calls: list[bool] = []

    async def warmup() -> None:
        calls.append(True)

    monkeypatch.setattr(engine._proxy, "warmup", warmup)

    await engine.warmup()

    assert calls == [True]


async def test_synthesize_forwards_to_tts() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["json"] = _payload(request)
        return httpx.Response(200, content=WAV_BYTES)

    engine = _engine(handler)
    result = await engine.synthesize(
        text="hola", voice_id="chatterbox-default", language="es", speed=1.2
    )
    assert result == WAV_BYTES
    assert captured["path"] == "/api/v1/tts"
    assert captured["json"] == {
        "text": "hola",
        "voice_id": "chatterbox-default",
        "language": "es",
        "speed": 1.2,
    }


async def test_clone_and_instruct_forward_to_their_endpoints() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, content=WAV_BYTES)

    engine = _engine(handler)
    await engine.synthesize_clone(
        text="hola", reference_audio_path="/app/storage/voices/1/ref.wav", language="es"
    )
    await engine.synthesize_instruct(
        text="hola", instruct="exaggeration=0.7 cfg_weight=0.3", language="es"
    )
    assert paths == ["/api/v1/tts/clone", "/api/v1/tts/instruct"]


async def test_health_check_reports_the_public_engine_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/health"
        return httpx.Response(200, json={"status": "ok", "mode": "REAL", "model_loaded": True})

    engine = _engine(handler)
    health = await engine.health_check()
    assert health["engine"] == "chatterbox"
    assert health["model_loaded"] is True
    assert health["mode"] == "REAL"


async def test_health_check_without_sidecar_is_not_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHATTERBOX_REMOTE_URL", raising=False)
    engine = ChatterboxRemoteEngine()
    health = await engine.health_check()
    assert health["status"] == "unavailable"
    assert health["engine"] == "chatterbox"
    assert health["model_loaded"] is False


async def test_list_stock_voices_comes_from_the_sidecar() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/voices/stock"
        return httpx.Response(200, json=[{"voice_id": "chatterbox-default", "language": "es"}])

    engine = _engine(handler)
    voices = await engine.list_stock_voices("es")
    assert voices == [{"voice_id": "chatterbox-default", "language": "es"}]


def test_factory_uses_the_remote_class_when_the_url_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHATTERBOX_REMOTE_URL", SIDECAR_URL)
    engine = create_engine("chatterbox")
    assert isinstance(engine, ChatterboxRemoteEngine)
    assert engine.name == "chatterbox"
