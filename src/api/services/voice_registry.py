"""Unified voice registry: every selectable voice, tagged by engine (Phase 9.1).

Each TTS engine keeps its own voice vocabulary — Edge ShortNames, tts-api
gateway stock ids and clone UUIDs, Modal catalog profile names — and the GUI
used to show one engine at a time with no notion of where a voice came from.
The registry merges the three categories of every engine (``stock``,
``cloned``, ``generated``) into one :class:`VoiceOption` list so that:

* ``GET /api/admin/voices/all`` and the GUI dropdowns offer the full
  catalogue, each entry tagged with the engines that accept it and its origin;
* the multi-engine guard (Phase 9.2) can tell whether a selection is
  consistent before anything is generated.

A source that cannot be reached degrades to an empty list instead of failing
the page, exactly like the per-engine helpers did before.
"""

from __future__ import annotations

import asyncio

import structlog

from src.api.models.narration import TTSBackend
from src.api.models.voices import VoiceOption

logger = structlog.get_logger(__name__)

#: Categories a voice can belong to, in the order they are listed.
CATEGORY_ORDER: tuple[str, ...] = ("stock", "cloned", "generated")
_CATEGORY_RANK = {name: index for index, name in enumerate(CATEGORY_ORDER)}

#: Engines of the Modal catalog: one reference WAV serves both backends.
MODAL_ENGINES: tuple[str, ...] = (TTSBackend.QWEN3_TTS.value, TTSBackend.ZONOS2.value)


def _dedupe(voices: list[VoiceOption]) -> list[VoiceOption]:
    """Merge entries that repeat an id across sources.

    The same id can legitimately belong to two vocabularies (an edge/gateway
    alias), in which case the union of engines is what both sources mean.

    Args:
        voices: Raw merged entries in source order.

    Returns:
        One entry per id, with the engines unioned.
    """
    merged: dict[str, VoiceOption] = {}
    for voice in voices:
        known = merged.get(voice.id)
        if known is None:
            merged[voice.id] = voice
            continue
        engines = list(dict.fromkeys([*known.engines, *voice.engines]))
        merged[voice.id] = known.model_copy(update={"engines": engines})
    return list(merged.values())


async def gateway_options() -> tuple[list[str], list[VoiceOption]]:
    """Fetch the tts-api gateway voices (stock + clones).

    Returns:
        Tuple of ``(languages, voices)``. Both empty when the gateway is
        unreachable, so the page still renders.
    """
    from src.api.services.voices import VoicesService

    service = VoicesService()
    try:
        languages = await service.list_languages()
        stock = await service.list_stock()
        cloned = await service.list_cloned()
    except Exception as exc:  # noqa: BLE001 - a dead gateway must not break the page
        logger.warning("voice_registry_gateway_failed", error=str(exc))
        return [], []

    voices: list[VoiceOption] = []
    for category, entries in (("stock", stock), ("cloned", cloned)):
        for item in entries:
            identifier = str(item.get("voice_id") or item.get("name") or "").strip()
            if not identifier:
                continue
            voices.append(
                VoiceOption(
                    id=identifier,
                    label=str(item.get("name") or identifier),
                    language=str(item.get("language") or ""),
                    engines=[TTSBackend.OMNIVOICE.value],
                    category=category,
                )
            )
    return languages, voices


async def edge_options() -> tuple[list[str], list[VoiceOption]]:
    """Fetch the real edge-tts catalogue (ShortNames).

    Returns:
        Tuple of ``(languages, voices)``, where ``languages`` are the ISO
        639-1 prefixes of the locales. Both empty when the catalogue cannot
        be fetched (it is cached at module level by ``services.tts``).
    """
    from src.api.services import tts as tts_service

    try:
        # Populates tts_service._EDGE_VOICES; also caches the ShortName set.
        await asyncio.wait_for(tts_service._available_voice_names(), timeout=15.0)
    except Exception as exc:  # noqa: BLE001 - degrade to no voices, as before
        logger.warning("voice_registry_edge_failed", error=str(exc))
        return [], []

    voices: list[VoiceOption] = []
    locales: set[str] = set()
    for item in tts_service._EDGE_VOICES or []:
        short = str(item.get("ShortName") or "")
        locale = str(item.get("Locale") or "")
        if not short:
            continue
        if locale:
            locales.add(locale.split("-")[0])
        voices.append(
            VoiceOption(
                id=short,
                label=str(item.get("FriendlyName") or short),
                language=locale,
                engines=[TTSBackend.EDGE.value],
                category="stock",
            )
        )
    return sorted(locales), voices


def modal_options() -> tuple[list[str], list[VoiceOption]]:
    """Read the Modal catalog: committed profiles + operator-created voices.

    Returns:
        Tuple of ``(languages, voices)``. Both empty when the catalog file is
        missing or unreadable (a Modal dropdown then falls back to a text
        input, as before).
    """
    from src.core.voice_catalog import (
        VoiceCatalogError,
        load_custom_profiles,
        load_profiles,
    )

    try:
        profiles = load_profiles()
        custom = load_custom_profiles()
    except VoiceCatalogError as exc:
        logger.warning("voice_registry_modal_failed", error=str(exc))
        return [], []

    known = {profile.name for profile in profiles}
    voices = [
        VoiceOption(
            id=profile.name,
            label=profile.label or profile.name,
            language=profile.language,
            engines=list(MODAL_ENGINES),
            category="stock",
        )
        for profile in profiles
    ]
    voices.extend(
        VoiceOption(
            id=profile.name,
            label=profile.label or profile.name,
            language=profile.language,
            engines=list(MODAL_ENGINES),
            category="generated",
        )
        for profile in custom
        if profile.name not in known
    )
    languages = sorted({voice.language for voice in voices if voice.language})
    return languages, voices


async def list_all_voices(
    engine: str | None = None,
    category: str | None = None,
) -> list[VoiceOption]:
    """List every selectable voice of every engine.

    Args:
        engine: Optional canonical engine id ('edge', 'omni_voice', ...);
            keeps the voices whose id that engine accepts.
        category: Optional 'stock', 'cloned' or 'generated'.

    Returns:
        The merged voices, ordered by category, language and label.
    """
    _, edge = await edge_options()
    _, gateway = await gateway_options()
    _, modal = modal_options()

    voices = _dedupe([*edge, *gateway, *modal])
    if engine:
        wanted = engine.strip().lower()
        voices = [voice for voice in voices if wanted in voice.engines]
    if category:
        wanted = category.strip().lower()
        voices = [voice for voice in voices if voice.category == wanted]
    return sorted(
        voices,
        key=lambda voice: (
            _CATEGORY_RANK.get(voice.category, len(CATEGORY_ORDER)),
            voice.language,
            voice.label,
        ),
    )
