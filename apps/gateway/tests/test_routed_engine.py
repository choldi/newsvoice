"""Tests del router multi-engine (locations + engine override)."""

from __future__ import annotations

import pytest

from tts_api.core.engine_factory import create_engine
from tts_api.core.engines.edgetts_engine import EdgeTTSEngine
from tts_api.core.engines.mock_engine import MockEngine
from tts_api.core.engines.routed_engine import RoutedEngine


def _routed() -> RoutedEngine:
    engines = {"mock": MockEngine(), "edgetts": EdgeTTSEngine()}
    routing = {"es": "mock", "ca": "edgetts", "_default": "mock"}
    return RoutedEngine(engines=engines, routing=routing)


def test_routed_capabilities_union_locations() -> None:
    caps = _routed().capabilities
    # mock runs locally, edgetts runs in the cloud; the union advertises both.
    assert set(caps.locations) == {"local", "cloud"}
    assert caps.stock_voices is True


def test_resolve_engine_picks_language_routing() -> None:
    r = _routed()
    assert isinstance(r._resolve_engine("ca"), EdgeTTSEngine)
    assert isinstance(r._resolve_engine("es"), MockEngine)


def test_resolve_engine_honours_explicit_override() -> None:
    """A pinned engine wins over the language routing."""
    r = _routed()
    # 'ca' would route to edgetts, but the explicit pin forces mock.
    assert isinstance(r._resolve_engine("ca", engine_name="mock"), MockEngine)
    # Comma-separated tags resolve to the first known engine.
    assert isinstance(r._resolve_engine("es", engine_name="nominate,edgetts"), EdgeTTSEngine)


async def test_routed_synthesize_forwards_engine_name() -> None:
    r = _routed()
    # 'ca' routes to edgetts (which raises FeatureNotSupportedError for cloning),
    # so pinning 'mock' must bypass the language routing.
    wav = await r.synthesize_clone(
        text="hola",
        reference_audio_path="/nope.wav",
        language="ca",
        engine_name="mock",
    )
    assert isinstance(wav, bytes) and len(wav) > 0


def test_factory_creates_qwen3() -> None:
    assert create_engine("qwen3_tts").name == "qwen3_tts"


def test_factory_creates_routed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTS_ENGINES", '{"es":"mock","ca":"edgetts","_default":"mock"}')
    assert create_engine("routed").name.startswith("routed(")


async def test_list_stock_voices_skips_a_failing_sub_engine() -> None:
    """One unavailable sub-engine must not hide the voices of the others.

    ``omnivoice_remote`` answers 503-style errors when its sidecar is down;
    the union endpoint has to keep serving pocket/edge/kokoro voices.
    """

    class _Broken:
        name = "broken"

        async def list_stock_voices(self, language: str | None = None) -> list[dict]:
            from tts_api.core.exceptions import EngineUnavailableError

            raise EngineUnavailableError("sidecar caído")

    engines = {"mock": MockEngine(), "broken": _Broken()}
    routed = RoutedEngine(engines=engines, routing={"_default": "mock"})  # type: ignore[arg-type]

    voices = await routed.list_stock_voices()

    assert voices, "the healthy sub-engine still contributes its voices"
    assert all(v["voice_id"] for v in voices)


async def test_optional_engine_failure_does_not_degrade_health() -> None:
    """A missing cloud engine is reported but does not make routed health fail."""

    class _CloudDown:
        name = "fish_tts"

        async def health_check(self) -> dict:
            return {"model_loaded": False, "error": "401"}

    routed = RoutedEngine(
        engines={"mock": MockEngine(), "fish_tts": _CloudDown()},  # type: ignore[arg-type]
        routing={"_default": "mock"},
        optional_engines={"fish_tts"},
    )

    health = await routed.health_check()

    assert health["model_loaded"] is True
    assert "fish_tts" in health["sub_engines"]
    assert health["optional_engines"] == ["fish_tts"]


async def test_optional_engine_initialize_failure_is_non_fatal() -> None:
    """Routed startup survives an unconfigured optional sub-engine."""

    class _CantInit:
        name = "openai_tts"

        async def initialize(self) -> None:
            raise RuntimeError("missing key")

    routed = RoutedEngine(
        engines={"mock": MockEngine(), "openai_tts": _CantInit()},  # type: ignore[arg-type]
        routing={"_default": "mock"},
        optional_engines={"openai_tts"},
    )

    await routed.initialize()


async def test_routed_stock_voices_are_tagged_by_engine() -> None:
    """Stock voices expose which sub-engine produced them."""
    routed = RoutedEngine(
        engines={"mock": MockEngine()},
        routing={"_default": "mock"},
    )

    voices = await routed.list_stock_voices()

    assert voices
    assert all(v["engine"] == "mock" for v in voices)


async def test_routed_health_is_ok_when_every_sub_engine_reports_loaded() -> None:
    """Con todos los sub-engines sanos el agregado no sale ``degraded``."""
    health = await _routed().health_check()

    assert health["model_loaded"] is True
    assert health["mode"] == "ROUTED"
    assert set(health["sub_engines"]) == {"mock", "edgetts"}


async def test_routed_health_marks_a_crashing_sub_engine_as_not_loaded() -> None:
    """Un sub-engine que revienta su health se señala en el desglose."""

    class _Boom:
        name = "boom"

        async def health_check(self) -> dict:
            raise RuntimeError("fallo interno")

    routed = RoutedEngine(
        engines={"mock": MockEngine(), "boom": _Boom()},  # type: ignore[arg-type]
        routing={"_default": "mock"},
    )

    health = await routed.health_check()

    assert health["model_loaded"] is False
    assert health["sub_engines"]["boom"] == {"error": "fallo interno", "model_loaded": False}


async def test_routed_health_without_model_loaded_signal_degrades() -> None:
    """Un sub-engine que no expone la clave deja el agregado en ``False``.

    Es el contrato que obliga a cada engine a reportar ``model_loaded`` (fue
    exactamente el bug de ``omnivoice_remote``).
    """

    class _NoSignal:
        name = "no-signal"

        async def health_check(self) -> dict:
            return {"status": "ok"}

    routed = RoutedEngine(
        engines={"mock": MockEngine(), "no_signal": _NoSignal()},  # type: ignore[arg-type]
        routing={"_default": "mock"},
    )

    health = await routed.health_check()

    assert health["model_loaded"] is False
    assert "model_loaded" not in health["sub_engines"]["no_signal"]
