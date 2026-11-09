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
from collections.abc import Callable
from typing import Any

import structlog

from src.api.models.voices import VoiceProfile
from src.api.services.voice_catalog_admin import reference_audio
from src.api.services.voice_catalog_client import fetch_catalog
from src.api.services.voices import VoicesService
from src.core.voice_catalog import VoiceCatalogError, ref_text_for

logger = structlog.get_logger(__name__)

#: Engine pinned on the clone so ``/tts`` routes it to the local worker.
QWEN_ENGINE = "qwen3_tts"
#: Cloud engine that accepts inline reference audio for catalog profiles.
FISH_ENGINE = "fish_tts"

#: Clone name suffix: catalog voices may already be cloned for another engine
#: (e.g. ``pocket_tts``), and the gateway refuses duplicate names, so the
#: qwen3_tts clone is always stored under its own suffixed name.
CLONE_SUFFIX = "__qwen3_tts"
FISH_CLONE_SUFFIX = "__fish_tts"

_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: In-process cache of resolved profile names -> clone UUID. Reset on failure
#: so a transient error is retried on the next turn.
_RESOLVED: dict[str, str] = {}
_FISH_RESOLVED: dict[str, str] = {}

#: Serializes the first-resolution path so a multi-role render clones once.
_LOCK = asyncio.Lock()
_FISH_LOCK = asyncio.Lock()


def clone_name_for(voice: str) -> str:
    """Return the deterministic gateway clone name for a profile name.

    Args:
        voice: Catalog profile name.

    Returns:
        ``<voice>__qwen3_tts`` — always not reserved by a pocket_tts clone.
    """
    return f"{voice}{CLONE_SUFFIX}"


def fish_clone_name_for(voice: str) -> str:
    """Return the deterministic Fish gateway clone name for a profile name."""
    return f"{voice}{FISH_CLONE_SUFFIX}"


def _matches_engine(entry: dict[str, Any], engine: str) -> bool:
    """Report whether a cloned-voice listing entry was pinned on an engine."""
    engines = str(entry.get("engine") or "")
    return engine in {part.strip() for part in engines.split(",")}


def _entry_id(entry: dict[str, Any]) -> str:
    """Return the cloned-voice id of a listing entry.

    Args:
        entry: One item of ``GET /voices/cloned``.

    Returns:
        The entry's ``id`` or ``voice_id``; empty when neither is present.
    """
    return str(entry.get("id") or entry.get("voice_id") or "").strip()


async def _profile(name: str) -> VoiceProfile | None:
    """Return one catalog profile by name, tolerating an unreachable gateway.

    Args:
        name: Profile name.

    Returns:
        The ``VoiceProfile`` or None when unknown or unreadable.
    """
    try:
        snapshot = await fetch_catalog()
    except VoiceCatalogError as exc:
        logger.warning("gateway_voice_catalog_failed", error=str(exc))
        return None
    return next((profile for profile in snapshot.profiles if profile.name == name), None)


async def resolve_gateway_qwen_voice(voice: str, language: str) -> str:
    """Resolve one voice to a gateway id usable by the local qwen3_tts engine.

    Args:
        voice: Voice as configured (profile name, stock id or clone UUID).
        language: ISO 639-1 language code used when the profile has no
            language of its own.

    Returns:
        The gateway voice id to put in the ``/tts`` payload.
    """
    return await _resolve_engine_voice(
        voice,
        language,
        engine=QWEN_ENGINE,
        suffix=CLONE_SUFFIX,
        cache=_RESOLVED,
        lock=_LOCK,
        clone_name=clone_name_for,
    )


async def resolve_gateway_fish_voice(voice: str, language: str) -> str:
    """Resolve a catalog profile to a Fish Audio inline-clone id.

    Fish accepts the reference WAV inline, so catalog profiles can be cloned
    once via the gateway's normal clone endpoint (pinned to ``fish_tts``) and
    reused as a reference UUID. Stock ids, clone UUIDs and unknown ids pass
    through unchanged so the operator's Fish ``reference_id`` still works.

    Args:
        voice: Voice as configured (profile name, stock id or clone UUID).
        language: ISO 639-1 language code used when the profile has no
            language of its own.

    Returns:
        The gateway voice id to put in the ``/tts`` payload.
    """
    return await _resolve_engine_voice(
        voice,
        language,
        engine=FISH_ENGINE,
        suffix=FISH_CLONE_SUFFIX,
        cache=_FISH_RESOLVED,
        lock=_FISH_LOCK,
        clone_name=fish_clone_name_for,
    )


async def _resolve_engine_voice(
    voice: str,
    language: str,
    *,
    engine: str,
    suffix: str,
    cache: dict[str, str],
    lock: asyncio.Lock,
    clone_name: Callable[[str], str],
) -> str:
    """Shared profile -> engine clone resolution for clone-capable engines."""
    requested = (voice or "").strip()
    if not requested or _UUID.match(requested):
        return requested
    cached = cache.get(requested)
    if cached is not None:
        return cached

    profile = await _profile(requested)
    if profile is None and requested.endswith(suffix):
        return await _resolve_clone_name(requested, engine=engine, cache=cache, lock=lock)

    wav = await reference_audio(requested)
    if wav is None or profile is None:
        return requested

    async with lock:
        cached = cache.get(requested)
        if cached is not None:
            return cached
        try:
            service = VoicesService()
            found = await _find_registered(service, clone_name(requested), engine)
            if found:
                cache[requested] = found
                return found
            response = await service.clone_voice(
                name=clone_name(requested),
                language=profile.language or language,
                reference_audio=wav,
                filename=f"{requested}.wav",
                ref_text=ref_text_for(profile),
                engines=engine,
            )
            cloned_id = str(response.get("voice_id") or response.get("id") or "")
            if cloned_id:
                cache[requested] = cloned_id
                return cloned_id
            cache.pop(requested, None)
            return requested
        except Exception as exc:  # noqa: BLE001 - degrade to the caller's fallback
            logger.warning(
                "gateway_engine_clone_failed",
                voice=requested,
                engine=engine,
                error=str(exc),
            )
            cache.pop(requested, None)
            return requested


async def _resolve_clone_name(
    requested: str,
    *,
    engine: str,
    cache: dict[str, str],
    lock: asyncio.Lock,
) -> str:
    """Resolve a gateway clone name to its UUID for one engine."""
    async with lock:
        cached = cache.get(requested)
        if cached is not None:
            return cached
        try:
            found = await _find_registered(VoicesService(), requested, engine)
        except Exception as exc:  # noqa: BLE001 - degrade to the caller's fallback
            logger.warning(
                "gateway_clone_lookup_failed",
                voice=requested,
                engine=engine,
                error=str(exc),
            )
            return requested
        if found:
            cache[requested] = found
            return found
        return requested


async def _find_registered(service: VoicesService, wanted: str, engine: str) -> str | None:
    """Look up an already-registered engine clone by its gateway name."""
    for entry in await service.list_cloned():
        if str(entry.get("name") or "") == wanted and _matches_engine(entry, engine):
            return _entry_id(entry)
    return None
