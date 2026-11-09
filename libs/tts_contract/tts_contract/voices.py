"""Voice catalogue payloads: ``/voices/stock``, ``/voices/cloned``, clone/design.

Field names mirror the gateway's JSON exactly. Note that a cloned voice is keyed
by ``id`` while a fresh clone response is keyed by ``voice_id``; both live here
so callers do not have to guess which spelling an endpoint uses.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CloneVoiceResponse",
    "ClonedVoice",
    "DesignedVoice",
    "DesignedVoiceResponse",
    "StockVoice",
]


class StockVoice(BaseModel):
    """A predefined engine voice from ``GET /voices/stock``."""

    model_config = ConfigDict(extra="ignore")

    voice_id: str
    language: str = ""
    gender: str = ""
    name: str = ""
    engine: str = ""


class ClonedVoice(BaseModel):
    """A stored cloned voice row from ``GET /voices/cloned``.

    ``engine`` is a comma-separated string (e.g. ``"pocket_tts,omnivoice"``) and
    ``metadata`` is a free-form JSON object.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    name: str = ""
    language: str = ""
    reference_path: str = ""
    duration_sec: float = 0.0
    engine: str = ""
    created_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class CloneVoiceResponse(BaseModel):
    """The ``201`` body of ``POST /voices/clone``."""

    model_config = ConfigDict(extra="ignore")

    voice_id: str
    name: str = ""
    language: str = ""
    engines: str = ""
    ref_text: str | None = None
    message: str = ""


class DesignedVoice(BaseModel):
    """A stored instruct-based voice from ``GET /voices/designed``."""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    name: str = ""
    instruct: str = ""
    language: str = ""
    created_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class DesignedVoiceResponse(BaseModel):
    """The ``201`` body of ``POST /voices/design``."""

    model_config = ConfigDict(extra="ignore")

    voice_id: str
    name: str = ""
    instruct: str = ""
    language: str = ""
    message: str = ""
