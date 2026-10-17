"""Resolve catalog voices to local gateway qwen3_tts clone ids (Phase 10.5).

The gateway engine ``qwen3_tts`` has no stock voices: it can only synthesize
for voices registered as *clones* of a reference audio. Catalog profiles — the
committed ones and the operator-designed ones (``custom_profiles.json``) — all
carry such a reference, so the first time a profile name reaches the local
engine we register the reference as a gateway clone pinned to ``qwen3_tts``
and remember the UUID the gateway assigns. Later turns reuse that mapping; the
gateway's cloned listing is the source of truth, so a cold process re-discovers
an already-registered voice instead of re-cloning it.

Gateway-owned ids (stock ids and clone UUIDs) always pass through unchanged.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import structlog

from src.api.models.voices import VoiceProfile
from src.api.services.voice_catalog_admin import reference_wav
from src.api.services.voices import VoicesService
from src.core.voice_catalog import (
    VoiceCatalogError,
    load_profiles,
    ref_text_for,
)

logger = structlog.get_logger(__name__)

#: Engine pinned on the clone so ``/tts`` routes it to the local worker.
QWEN_ENGINE = "qwen3_tts"

#: Clone name suffix: catalog voices may already be cloned for another engine
#: (e.g. ``pocket_tts``), and the gateway refuses duplicate names, so the
#: qwen3_tts clone is always stored under its own suffixed name.
CLONE_SUFFIX = "__qwen3_tts"

_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: In-process cache of resolved profile names -> clone UUID. Reset on failure
#: so a transient error is retried on the next turn.
_RESOLVED: dict[str, str] = {}

#: Serializes the first-resolution path so a multi-role render clones once.
_LOCK = asyncio.Lock()


def clone_name_for(voice: str) -> str:
    """Return the deterministic gateway clone name for a profile name.

    Args:
        voice: Catalog profile name.

    Returns:
        ``<voice>__qwen3_tts`` — always not reserved by a pocket_tts clone.
    """
    return f"{voice}{CLONE_SUFFIX}"


def _matches_qwen(entry: dict[str, Any]) -> bool:
    """Report whether a cloned-voice listing entry was pinned on qwen3_tts.

    Args:
        entry: One item of ``GET /voices/cloned``.

    Returns:
        True when the entry's ``engine`` field contains ``qwen3_tts``.
    """
    engines = str(entry.get("engine") or "")
    return QWEN_ENGINE in {part.strip() for part in engines.split(",")}


def _entry_id(entry: dict[str, Any]) -> str:
    """Return the cloned-voice id of a listing entry.

    Args:
        entry: One item of ``GET /voices/cloned``.

    Returns:
        The entry's ``id`` or ``voice_id``; empty when neither is present.
    """
    return str(entry.get("id") or entry.get("voice_id") or "").strip()


def _profile(name: str) -> VoiceProfile | None:
    """Return one catalog profile by name, tolerating a broken catalog.

    Args:
        name: Profile name.

    Returns:
        The ``VoiceProfile`` or None when unknown/unreadable.
    """
    try:
        return next((p for p in load_profiles() if p.name == name), None)
    except VoiceCatalogError as exc:
        logger.warning("gateway_voice_catalog_failed", error=str(exc))
        return None


async def resolve_gateway_qwen_voice(voice: str, language: str) -> str:
    """Resolve one voice to a gateway id usable by the local qwen3_tts engine.

    Gateway-native ids (stock ids and clone UUIDs) are returned unchanged. A
    catalog profile name is registered once as a ``qwen3_tts`` clone of its
    reference WAV; the returned clone UUID then drives the synthesis. When
    registration is impossible (no reference, unreachable gateway, gateway
    error) the voice is returned unchanged so the caller keeps its usual
    failure/fallback behaviour.

    Args:
        voice: Voice as configured (profile name, stock id or clone UUID).
        language: ISO 639-1 language code used when the profile has no
            language of its own.

    Returns:
        The gateway voice id to put in the ``/tts`` payload.
    """
    requested = (voice or "").strip()
    if not requested or _UUID.match(requested):
        return requested
    cached = _RESOLVED.get(requested)
    if cached is not None:
        return cached
    wav = reference_wav(requested)
    profile = _profile(requested)
    if wav is None or profile is None:
        return requested

    async with _LOCK:
        cached = _RESOLVED.get(requested)
        if cached is not None:
            return cached
        try:
            service = VoicesService()
            found = await _find_registered(service, requested)
            if found:
                _RESOLVED[requested] = found
                return found
            response = await service.clone_voice(
                name=clone_name_for(requested),
                language=profile.language or language,
                reference_audio=wav.read_bytes(),
                filename=wav.name,
                ref_text=ref_text_for(profile),
                engines=QWEN_ENGINE,
            )
            cloned_id = str(response.get("voice_id") or response.get("id") or "")
            if cloned_id:
                _RESOLVED[requested] = cloned_id
                return cloned_id
            _RESOLVED.pop(requested, None)
            return requested
        except Exception as exc:  # noqa: BLE001 - degrade to the caller's fallback
            logger.warning(
                "gateway_qwen_clone_failed",
                voice=requested,
                error=str(exc),
            )
            _RESOLVED.pop(requested, None)
            return requested


async def _find_registered(service: VoicesService, voice: str) -> str | None:
    """Look up an already-registered qwen3_tts clone of a profile.

    Args:
        service: Gateway voices client.
        voice: Catalog profile name.

    Returns:
        The clone UUID, or None when no qwen3_tts clone exists yet.
    """
    wanted = clone_name_for(voice)
    for entry in await service.list_cloned():
        if str(entry.get("name") or "") == wanted and _matches_qwen(entry):
            return _entry_id(entry)
    return None
