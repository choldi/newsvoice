"""Factory for creating TTS engine instances based on TTS_ENGINE setting."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from tts_api.core.engine_base import TtsEngineBase
from tts_api.settings import get_settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def _create_single_engine(name: str) -> TtsEngineBase:
    """Create a single engine instance by name."""
    name = name.lower().strip()

    if name == "mock":
        from tts_api.core.engines.mock_engine import MockEngine

        engine_class: type[TtsEngineBase] = MockEngine
    elif name == "pocket_tts":
        from tts_api.core.engines.pocket_tts_engine import PocketTTSEngine

        engine_class = PocketTTSEngine
    elif name == "edgetts":
        from tts_api.core.engines.edgetts_engine import EdgeTTSEngine

        engine_class = EdgeTTSEngine
    elif name == "qwen3_tts":
        from tts_api.core.engines.qwen3_tts_engine import Qwen3TTSEngine

        engine_class = Qwen3TTSEngine
    elif name == "omnivoice":
        from tts_api.core.engines.omnivoice_engine_adapter import OmniVoiceAdapter

        engine_class = OmniVoiceAdapter
    elif name == "omnivoice_remote":
        from tts_api.core.engines.omnivoice_remote_engine import OmniVoiceRemoteEngine

        engine_class = OmniVoiceRemoteEngine
    elif name == "zono2":
        from tts_api.core.engines.zono2_engine import Zono2Engine

        engine_class = Zono2Engine
    elif name == "kokoro":
        from tts_api.core.engines.kokoro_engine import KokoroEngine

        engine_class = KokoroEngine
    elif name == "piper":
        from tts_api.core.engines.piper_engine import PiperEngine

        engine_class = PiperEngine
    elif name == "chatterbox":
        from tts_api.core.engines.chatterbox_engine import ChatterboxEngine

        engine_class = ChatterboxEngine
    else:
        raise ValueError(f"Engine desconocido: '{name}'")

    return engine_class()


def _create_routed_engine(routing_json: str, extra_engines: str = "") -> TtsEngineBase:
    """Create a RoutedEngine from a JSON routing configuration.

    The routing JSON maps language codes to engine names:
        {"es":"pocket_tts","en":"pocket_tts","ca":"edgetts","_default":"pocket_tts"}

    ``extra_engines`` is a comma-separated list of engines to instantiate
    without routing any language to them, so per-voice ``engine`` pins can
    target them (e.g. ``omnivoice_remote``).
    """
    from tts_api.core.engines.routed_engine import RoutedEngine

    if not routing_json or not routing_json.strip():
        raise ValueError(
            "TTS_ENGINE=routed requiere TTS_ENGINES con el mapeo JSON de idiomas. "
            'Ejemplo: TTS_ENGINES=\'{"es":"pocket_tts","ca":"edgetts","_default":"pocket_tts"}\''
        )

    try:
        routing = json.loads(routing_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"TTS_ENGINES no es JSON válido: {e}") from e

    if not isinstance(routing, dict):
        raise ValueError("TTS_ENGINES debe ser un dict JSON {idioma: engine_name}")

    # Collect unique engine names needed
    engine_names: set[str] = set(routing.values())
    engine_names.discard("_default")
    engine_names.update(name.strip() for name in extra_engines.split(",") if name.strip())

    if not engine_names:
        raise ValueError("TTS_ENGINES no contiene ningún engine válido")

    # Create engine instances (one per unique engine name)
    engines: dict[str, TtsEngineBase] = {}
    for name in engine_names:
        engines[name] = _create_single_engine(name)

    logger.info(
        "Created routed engine: routing=%s, sub-engines=%s",
        routing,
        list(engines.keys()),
    )

    return RoutedEngine(engines=engines, routing=routing)


def create_engine(engine_name: str | None = None) -> TtsEngineBase:
    """Create and return a TTS engine instance.

    Args:
        engine_name: Engine identifier. If None, reads from TTS_ENGINE env var.

    Returns:
        An initialized TtsEngineBase implementation.

    Raises:
        ValueError: If the engine name is not recognized.
    """
    if engine_name is None:
        settings = get_settings()
        engine_name = settings.TTS_ENGINE

    engine_name = engine_name.lower().strip()

    if engine_name == "routed":
        settings = get_settings()
        return _create_routed_engine(settings.TTS_ENGINES, settings.TTS_EXTRA_ENGINES)

    return _create_single_engine(engine_name)
