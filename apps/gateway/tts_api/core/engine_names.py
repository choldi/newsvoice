"""Canonical TTS engine identifiers used by the gateway.

The names here are the single runtime catalog: settings validation, the
``/engines`` endpoint and the factory all consume this tuple. ``routed`` is a
selection strategy, not an instantiable engine.
"""

from __future__ import annotations

SINGLE_ENGINE_NAMES = (
    "mock",
    "pocket_tts",
    "edgetts",
    "qwen3_tts",
    "omnivoice",
    "omnivoice_remote",
    "zono2",
    "kokoro",
    "piper",
    "chatterbox",
    "fish_tts",
    "openai_tts",
)
ROUTED_ENGINE_NAME = "routed"
ALL_ENGINE_NAMES = (*SINGLE_ENGINE_NAMES, ROUTED_ENGINE_NAME)

__all__ = ["ALL_ENGINE_NAMES", "ROUTED_ENGINE_NAME", "SINGLE_ENGINE_NAMES"]
