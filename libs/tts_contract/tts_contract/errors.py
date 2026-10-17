"""Error bodies the tts-api gateway answers with.

FastAPI wraps an ``HTTPException`` detail as ``{"detail": ...}``. The gateway
uses two variants: a *nested* object (``{"detail": {"detail": ..., "error_type":
...}}``) for its typed engine errors, and a *flat* string for ad-hoc 4xx/5xx
(``{"detail": "Voz clonada no encontrada: ..."}``). The auth middleware answers
top-level ``{"detail": ..., "error_type": "unauthorized"}`` and slowapi answers
``{"error": "Rate limit exceeded: ..."}``.

:func:`parse_gateway_error` normalises all four shapes into a single
:class:`GatewayErrorInfo`, so clients stop re-implementing the unwrapping.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "GatewayErrorBody",
    "GatewayErrorDetail",
    "GatewayErrorInfo",
    "TokenSuggestion",
    "parse_gateway_error",
]


class TokenSuggestion(BaseModel):
    """One invalid instruct token plus the gateway's suggested replacement."""

    model_config = ConfigDict(extra="ignore")

    token: str
    suggestion: str | None = None


class GatewayErrorDetail(BaseModel):
    """The object the gateway nests inside ``{"detail": ...}``.

    Every field beyond ``detail``/``error_type`` is optional: which ones are
    present depends on the error type (voice, language, instruct, emotion,
    feature or engine availability).
    """

    model_config = ConfigDict(extra="ignore")

    detail: str = ""
    error_type: str = ""
    voice_id: str | None = None
    voice_type: str | None = None
    valid_voice_ids: list[str] | None = None
    language: str | None = None
    supported_languages: list[str] | None = None
    emotion: str | None = None
    supported_emotions: list[str] | None = None
    invalid_items: list[TokenSuggestion] | None = None
    valid_tokens: list[str] | None = None
    feature: str | None = None
    engine: str | None = None


class GatewayErrorBody(BaseModel):
    """Envelope for any gateway error answer.

    ``detail`` is either the nested object or a flat string, so a union absorbs
    both. ``error_type`` at the top level and ``error`` cover the middleware and
    rate-limit shapes respectively.
    """

    model_config = ConfigDict(extra="ignore")

    detail: str | GatewayErrorDetail = ""
    error_type: str = ""
    error: str = ""


class GatewayErrorInfo(BaseModel):
    """Normalised gateway error: message, machine type and suggested voices."""

    model_config = ConfigDict(extra="ignore")

    message: str = ""
    error_type: str = ""
    valid_voice_ids: list[str] = Field(default_factory=list)


def parse_gateway_error(body: Any, *, status_code: int = 0) -> GatewayErrorInfo:
    """Unwrap any of the gateway error shapes into a normalised result.

    Tolerates a non-dict body (an HTML error page), the nested and flat
    ``detail`` variants, the middleware's top-level ``error_type`` and the rate
    limiter's ``error`` field. A body that carries nothing usable still yields a
    message naming the status code; when the gateway offered alternative voice
    ids they are appended to the message.

    Args:
        body: The already-decoded JSON body, or ``None`` when decoding failed.
        status_code: HTTP status, used only for the fallback message.

    Returns:
        A populated :class:`GatewayErrorInfo`.
    """
    info = GatewayErrorInfo()
    if isinstance(body, dict):
        try:
            parsed = GatewayErrorBody.model_validate(body)
        except ValidationError:  # pragma: no cover - all fields are optional
            parsed = GatewayErrorBody()
        detail = parsed.detail
        if isinstance(detail, GatewayErrorDetail):
            info.error_type = detail.error_type or parsed.error_type
            info.message = detail.detail
            if detail.valid_voice_ids:
                info.valid_voice_ids = [str(item) for item in detail.valid_voice_ids]
        else:
            info.message = detail
            info.error_type = parsed.error_type
        if not info.message:
            info.message = parsed.error
    if not info.message:
        info.message = f"gateway returned {status_code}"
    if info.valid_voice_ids:
        suggestions = ", ".join(info.valid_voice_ids[:8])
        info.message = f"{info.message} (valid voice ids: {suggestions})"
    return info
