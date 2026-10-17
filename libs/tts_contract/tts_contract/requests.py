"""Request bodies clients POST to the gateway.

``POST /tts`` and ``POST /conversations`` answer with WAV bytes, so the JSON
surface they share with clients is the *request*. Modelling it means a client
cannot silently start sending a field the gateway will not read.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = ["ConversationRequest", "ConversationTurn", "TTSRequest"]


class TTSRequest(BaseModel):
    """Body of ``POST /tts``.

    ``emotion`` and ``engine`` are optional: a client omits them rather than
    sending ``null``, so serialize with ``model_dump(exclude_none=True)``.
    """

    model_config = ConfigDict(extra="ignore")

    text: str
    voice_id: str
    language: str = ""
    speed: float = 1.0
    emotion: str | None = None
    engine: str | None = None


class ConversationTurn(BaseModel):
    """One speaker's line in ``POST /conversations``."""

    model_config = ConfigDict(extra="ignore")

    voice_id: str
    text: str
    language: str = "es"


class ConversationRequest(BaseModel):
    """Body of ``POST /conversations``.

    ``stream`` travels as a query parameter, not in the body, so it is not
    modelled here.
    """

    model_config = ConfigDict(extra="ignore")

    # The gateway enforces its own minimum (2 turns); the contract only
    # serialises, so a too-short request still reaches it as a 400.
    turns: list[ConversationTurn]
    pause_ms: int = 300
    speed: float = 1.0
