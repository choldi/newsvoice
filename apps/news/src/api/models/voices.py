"""Pydantic models for the voice catalog (Phase 7.8).

The catalog itself (profiles, manifest, drift rules) is a contract shared with
the gateway, which owns the files since P6: :class:`VoiceProfile`,
:class:`VoiceManifestEntry`, :class:`VoiceSource` and :func:`is_voice_slug`
are re-exported from :mod:`tts_contract.catalog` so both apps validate the same
JSON with the same rules. This module keeps what is news-engine's own concern:
the dropdown shapes and the REST payloads of the admin GUI.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from tts_contract import (
    VoiceManifestEntry,
    VoiceProfile,
    VoiceSource,
    is_voice_slug,
)

__all__ = [
    "CustomVoiceIn",
    "CustomVoiceOut",
    "VoiceManifestEntry",
    "VoiceOption",
    "VoiceProfile",
    "VoiceSource",
    "VoicesAllOut",
    "is_voice_slug",
]


class VoiceOption(BaseModel):
    """One selectable voice from any engine, tagged for the GUI (Phase 9.1).

    The registry behind ``GET /api/admin/voices/all`` merges the three sources
    (cloned, generated, stock) of every engine into this one shape, so a
    dropdown can show where each voice lives and the engine guard can reject a
    selection that mixes engines.

    Attributes:
        id: Engine-native voice id: Edge ShortName, gateway stock id or clone
            UUID, or catalog profile name.
        label: Human-readable name shown in the dropdown.
        language: Language code in the engine's own vocabulary ('es', 'es-ES').
        engines: Engines that accept this id. A Modal catalog voice serves
            qwen3_tts, zonos2 and voxcpm2; an edge/gateway alias id serves both.
        category: 'stock' (shipped by an engine), 'cloned' (gateway clone) or
            'generated' (custom voice created from the GUI).
    """

    id: str = Field(..., min_length=1, description="Engine-native voice id")
    label: str = Field(..., min_length=1, description="Human-readable dropdown label")
    language: str = Field(default="", description="Language code as the engine spells it")
    engines: list[str] = Field(
        default_factory=list, description="Engines whose vocabulary contains this id"
    )
    category: str = Field(..., description="stock | cloned | generated")


class VoicesAllOut(BaseModel):
    """Response carrying every selectable voice, tagged by engine and origin."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    voices: list[VoiceOption] = Field(..., description="Cloned + generated + stock voices")


class CustomVoiceIn(BaseModel):
    """Request to create an operator-authored voice profile (Phase 9.3).

    The service validates the payload as a full :class:`VoiceProfile`; this
    model only shapes the transport, so the GUI form and the REST endpoint
    share one contract.

    Attributes:
        name: Stable slug; becomes the reference file name on disk.
        label: Human-readable name (defaults to the name when empty).
        language: ISO 639-1 code the voice speaks.
        instruct: Natural-language persona description for VoiceDesign.
        ref_segments: Reference text split in segments (one phrase per line
            in the form). May be empty when ``ref_text`` carries the text.
        ref_text: Whole transcript, used when no segments were given.
        role_hint: Intended use (anchor, cohost, narrator, ...).
        generate: Start the design workflow right after saving the profile.
        upload: Also upload the reference to the Modal Volume when starting.
    """

    name: str = Field(..., min_length=1, max_length=64, description="Stable slug")
    label: str = Field(default="", max_length=128, description="Human label")
    language: str = Field(default="es", description="ISO 639-1 code")
    instruct: str = Field(..., min_length=10, max_length=2000, description="VoiceDesign persona")
    ref_segments: list[str] = Field(default_factory=list, description="Transcript lines")
    ref_text: str = Field(default="", max_length=600, description="Whole transcript")
    role_hint: str = Field(default="", max_length=64, description="Intended role")
    generate: bool = Field(default=True, description="Queue the design workflow")
    upload: bool = Field(default=False, description="Upload the reference to Modal")


class CustomVoiceOut(BaseModel):
    """Response for a created custom voice (Phase 9.3)."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    profile: dict[str, Any] = Field(..., description="The saved profile")
    workflow_id: str = Field(default="", description="Design workflow id when started")
    start_error: str = Field(
        default="", description="Why the design run could not be queued (profile kept)"
    )
