"""Engine catalogue payload for ``GET /engines``.

Describes which engines the gateway knows, where each may run (``local`` on the
gateway, ``cloud`` on Modal) and what it supports, so clients (notably
news-engine) can render the "run in modal" switch.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CANONICAL_ENGINE_IDS",
    "LEGACY_ENGINE_ALIASES",
    "EngineCapabilities",
    "EngineDescriptor",
    "EnginesResponse",
    "canonical_engine_id",
]

CANONICAL_ENGINE_IDS: tuple[str, ...] = (
    "edgetts",
    "gateway",
    "omnivoice",
    "omnivoice_remote",
    "pocket_tts",
    "piper",
    "chatterbox",
    "qwen3_tts",
    "zono2",
    "voxcpm2",
    "kokoro",
    "fish_tts",
    "openai_tts",
)

LEGACY_ENGINE_ALIASES: dict[str, str] = {
    "edge": "edgetts",
    "edgetts": "edgetts",
    "omni_voice": "omnivoice",
    "omnivoice": "omnivoice",
    "tts_api": "omnivoice",
    "gateway": "gateway",
    "gateway_engine": "gateway",
    "qwen3": "qwen3_tts",
    "qwen3_tts": "qwen3_tts",
    "zonos2": "zono2",
    "zono2": "zono2",
    "voxcpm": "voxcpm2",
    "voxcpm2": "voxcpm2",
    "kokoro": "kokoro",
    "fish": "fish_tts",
    "fish_audio": "fish_tts",
    "fish_tts": "fish_tts",
    "openai": "openai_tts",
    "openai_tts": "openai_tts",
}


def canonical_engine_id(value: str | None) -> str:
    """Normalize a configured engine id to its canonical form.

    Args:
        value: Raw engine id from configuration, forms or stored payloads.

    Returns:
        The canonical id, or an empty string when no id was supplied.
    """
    raw = (value or "").strip().lower()
    return LEGACY_ENGINE_ALIASES.get(raw, raw)


class EngineCapabilities(BaseModel):
    """What a single engine can do."""

    model_config = ConfigDict(extra="ignore")

    stock_voices: bool = False
    voice_cloning: bool = False
    instruct_voice_design: bool = False
    emotions: bool = False
    streaming: bool = False
    supported_languages: list[str] = Field(default_factory=list)


class EngineDescriptor(BaseModel):
    """One engine's catalogue entry.

    ``capabilities`` is ``None`` when the engine could not be introspected, in
    which case ``available`` is ``False`` and ``error`` explains why.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    available: bool = False
    locations: list[str] = Field(default_factory=list)
    capabilities: EngineCapabilities | None = None
    error: str | None = None
    label: str | None = None
    aliases: list[str] = Field(default_factory=list)
    execution_mode: Literal["gateway", "modal", "local", "meta"] = "gateway"
    default_output_format: Literal[".mp3", ".wav"] | None = None


class EnginesResponse(BaseModel):
    """The ``GET /engines`` body."""

    model_config = ConfigDict(extra="ignore")

    status: str = "ok"
    active: str = ""
    engines: list[EngineDescriptor] = Field(default_factory=list)
