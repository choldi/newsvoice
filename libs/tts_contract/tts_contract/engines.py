"""Engine catalogue payload for ``GET /engines``.

Describes which engines the gateway knows, where each may run (``local`` on the
gateway, ``cloud`` on Modal) and what it supports, so clients (notably
news-engine) can render the "run in modal" switch.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["EngineCapabilities", "EngineDescriptor", "EnginesResponse"]


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


class EnginesResponse(BaseModel):
    """The ``GET /engines`` body."""

    model_config = ConfigDict(extra="ignore")

    status: str = "ok"
    active: str = ""
    engines: list[EngineDescriptor] = Field(default_factory=list)
