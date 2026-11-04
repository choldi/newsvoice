"""Tests for the OmniVoice HTTP proxy engine (sidecar bridge)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tts_api.core.engines.omnivoice_remote_engine import OmniVoiceRemoteEngine
from tts_api.core.exceptions import EngineUnavailableError

SIDECAR_URL = "http://omnivoice-sidecar:8000"
WAV_BYTES = b"RIFF....WAVEfmt "


def _engine(handler: Callable[[httpx.Request], httpx.Response]) -> OmniVoiceRemoteEngine:
    """Build a proxy engine whose client is bound to a MockTransport."""
    engine = OmniVoiceRemoteEngine(base_url=SIDECAR_URL)
    engine._client = httpx.AsyncClient(base_url=SIDECAR_URL, transport=httpx.MockTransport(handler))
    engine._available = True
    return engine


def _payload(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode())


def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Make ``initialize`` build a client backed by a MockTransport."""
    real_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("tts_api.core.engines.omnivoice_remote_engine.httpx.AsyncClient", factory)


async def test_capabilities() -> None:
    engine = OmniVoiceRemoteEngine(base_url=SIDECAR_URL)
    caps = engine.capabilities
    assert engine.name == "omnivoice_remote"
    assert caps.stock_voices is True
    assert caps.voice_cloning is True
    assert caps.instruct_voice_design is True
    assert caps.emotions is True
    assert "es" in caps.supported_languages


async def test_initialize_without_url_stays_unavailable() -> None:
    engine = OmniVoiceRemoteEngine(base_url="")
    await engine.initialize()
    assert engine._available is False
    with pytest.raises(EngineUnavailableError):
        await engine.synthesize(text="hi", voice_id="x", language="en")


async def test_initialize_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/health/live"
        return httpx.Response(200, json={"status": "ok"})

    _patch_client(monkeypatch, handler)
    engine = OmniVoiceRemoteEngine(base_url=SIDECAR_URL)
    await engine.initialize()
    assert engine._available is True
    await engine.close()


async def test_initialize_unreachable_sidecar_is_non_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _patch_client(monkeypatch, handler)
    engine = OmniVoiceRemoteEngine(base_url=SIDECAR_URL)
    await engine.initialize()
    assert engine._available is False
    assert await engine.health_check() == {
        "status": "unavailable",
        "engine": "omnivoice_remote",
        "url": SIDECAR_URL,
        "model_loaded": False,
    }
    with pytest.raises(EngineUnavailableError):
        await engine.synthesize(text="hi", voice_id="x", language="en")
    await engine.close()


async def test_synthesize_posts_to_tts() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["json"] = _payload(request)
        return httpx.Response(200, content=WAV_BYTES)

    engine = _engine(handler)
    result = await engine.synthesize(text="hola", voice_id="v1", language="es", emotion="calm")
    assert result == WAV_BYTES
    assert captured["path"] == "/api/v1/tts"
    assert captured["json"] == {
        "text": "hola",
        "voice_id": "v1",
        "language": "es",
        "speed": 1.0,
        "emotion": "calm",
    }


async def test_synthesize_clone_posts_to_tts_clone() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["json"] = _payload(request)
        return httpx.Response(200, content=WAV_BYTES)

    engine = _engine(handler)
    result = await engine.synthesize_clone(
        text="hola",
        reference_audio_path="/app/storage/voices/1/reference.wav",
        language="es",
        ref_text="hola",
    )
    assert result == WAV_BYTES
    assert captured["path"] == "/api/v1/tts/clone"
    assert captured["json"]["reference_audio_path"] == "/app/storage/voices/1/reference.wav"
    assert captured["json"]["ref_text"] == "hola"


async def test_synthesize_clone_omits_empty_ref_text() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = _payload(request)
        return httpx.Response(200, content=WAV_BYTES)

    engine = _engine(handler)
    await engine.synthesize_clone(
        text="hola", reference_audio_path="/x.wav", language="es", ref_text=None
    )
    assert "ref_text" not in captured["json"]


async def test_synthesize_instruct_posts_to_tts_instruct() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["json"] = _payload(request)
        return httpx.Response(200, content=WAV_BYTES)

    engine = _engine(handler)
    await engine.synthesize_instruct(text="hola", instruct="male, calm", language="es")
    assert captured["path"] == "/api/v1/tts/instruct"
    assert captured["json"]["instruct"] == "male, calm"


async def test_list_stock_voices() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/voices/stock"
        assert request.url.params.get("language") == "es"
        return httpx.Response(
            200,
            json=[{"voice_id": "v1", "language": "es", "gender": "m", "name": "Uno"}],
        )

    engine = _engine(handler)
    voices = await engine.list_stock_voices(language="es")
    assert voices == [{"voice_id": "v1", "language": "es", "gender": "m", "name": "Uno"}]


async def test_post_error_raises_engine_unavailable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    engine = _engine(handler)
    with pytest.raises(EngineUnavailableError, match="rechazó"):
        await engine.synthesize(text="hi", voice_id="v", language="en")


async def test_connection_error_raises_engine_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    engine = _engine(handler)
    with pytest.raises(EngineUnavailableError):
        await engine.synthesize(text="hi", voice_id="v", language="en")


async def test_health_check_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/health"
        return httpx.Response(200, json={"engine": "omnivoice", "ready": True})

    engine = _engine(handler)
    health = await engine.health_check()
    assert health["status"] == "ok"
    assert health["engine"] == "omnivoice_remote"
    assert health["remote"]["ready"] is True
    assert health["model_loaded"] is True


async def test_health_check_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    engine = _engine(handler)
    health = await engine.health_check()
    assert health["status"] == "error"
    assert health["model_loaded"] is False


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # Sidecar real y sano.
        ({"status": "ok", "mode": "REAL"}, True),
        # El sidecar responde 200 sin señales explícitas: se asume disponible.
        ({"engine": "omnivoice", "ready": True}, True),
        # Sidecar con el modelo cargado explícitamente.
        ({"status": "ok", "mode": "REAL", "model_loaded": True}, True),
        # Sidecar en MOCK o degradado: no hay modelo real cargado.
        ({"status": "ok", "mode": "MOCK"}, False),
        ({"status": "degraded", "mode": "ROUTED"}, False),
        ({"status": "ok", "mode": "REAL", "model_loaded": False}, False),
    ],
)
async def test_health_check_model_loaded_is_derived_from_sidecar(
    payload: dict[str, Any],
    expected: bool,
) -> None:
    """``model_loaded`` sale del payload del sidecar, que no siempre lo trae.

    ``RoutedEngine`` une los sub-engines con ``all()``: sin esta clave el
    gateway entero respondía ``degraded``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/health"
        return httpx.Response(200, json=payload)

    engine = _engine(handler)
    health = await engine.health_check()

    assert health["model_loaded"] is expected


def test_remote_engine_is_clone_capable() -> None:
    """The routed gateway accepts voices pinned to the sidecar engine."""
    from tts_api.services.voice_service import CLONE_CAPABLE_ENGINES

    assert "omnivoice_remote" in CLONE_CAPABLE_ENGINES


async def test_close_releases_client() -> None:
    engine = _engine(lambda _request: httpx.Response(200, json={}))
    await engine.close()
    assert engine._client is None
    assert engine._available is False


async def test_routed_health_with_healthy_sidecar_is_not_degraded() -> None:
    """Regresión: sin ``model_loaded`` el routed agregado salía ``degraded``."""
    from tts_api.core.engines.mock_engine import MockEngine
    from tts_api.core.engines.routed_engine import RoutedEngine

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "mode": "REAL"})

    routed = RoutedEngine(
        engines={"mock": MockEngine(), "omnivoice_remote": _engine(handler)},
        routing={"_default": "mock"},
    )

    health = await routed.health_check()

    assert health["model_loaded"] is True
    assert health["sub_engines"]["omnivoice_remote"]["model_loaded"] is True


async def test_routed_health_with_sidecar_down_points_at_the_culprit() -> None:
    """Con el sidecar caído el gateway queda ``degraded`` y lo señala."""
    from tts_api.core.engines.mock_engine import MockEngine
    from tts_api.core.engines.routed_engine import RoutedEngine

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sidecar caído", request=request)

    routed = RoutedEngine(
        engines={"mock": MockEngine(), "omnivoice_remote": _engine(handler)},
        routing={"_default": "mock"},
    )

    health = await routed.health_check()

    assert health["model_loaded"] is False
    assert health["sub_engines"]["omnivoice_remote"] == {
        "status": "error",
        "engine": "omnivoice_remote",
        "error": "sidecar caído",
        "model_loaded": False,
    }
