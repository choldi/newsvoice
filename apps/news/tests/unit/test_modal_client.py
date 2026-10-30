"""Guards of the Modal voice-design client (Phase 10.4)."""

from __future__ import annotations

import pytest

from src.core import modal_client
from src.core.config import settings

_SEGMENTS = ["Hola, soy tu profe de ingles de siempre."]


async def test_design_voice_refuses_when_modal_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch cannot spend on Modal while the kill switch is off."""
    monkeypatch.setattr(settings, "modal_enabled", False)

    with pytest.raises(RuntimeError, match="modal is disabled"):
        await modal_client.design_voice(segments=_SEGMENTS, instruct="Femenina", language="es")


async def test_design_voice_requires_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabled without a token pair still fails before any network call."""
    monkeypatch.setattr(settings, "modal_enabled", True)
    monkeypatch.setattr(settings, "modal_token_id", "")
    monkeypatch.setattr(settings, "modal_token_secret", "")

    with pytest.raises(RuntimeError, match="credentials"):
        await modal_client.design_voice(segments=_SEGMENTS, instruct="Femenina", language="es")
