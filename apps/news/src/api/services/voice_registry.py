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

#: Engines of the Modal catalog: one reference WAV serves every Modal backend.
MODAL_ENGINES: tuple[str, ...] = (
    TTSBackend.QWEN3_TTS.value,
    TTSBackend.ZONOS2.value,
    TTSBackend.VOXCPM2.value,
)

#: Gateway sub-engine id used by each public backend that renders through the
#: gateway, so selecting ``kokoro`` loads the Kokoro catalogue directly.
_GATEWAY_BACKEND_PINS: dict[str, str] = {
    TTSBackend.KOKORO.value: "kokoro",
    TTSBackend.FISH.value: "fish_tts",
    TTSBackend.OPENAI.value: "openai_tts",
}

#: Gateway sub-engine name -> public backend value. ``pocket_tts`` et al. are
#: reached through the routed OmniVoice backend unless they have their own
#: public value (Kokoro, Fish, OpenAI).
_GATEWAY_ENGINE_VALUES: dict[str, str] = {
    "omnivoice": TTSBackend.OMNIVOICE.value,
    "omnivoice_remote": TTSBackend.OMNIVOICE.value,
    "edgetts": TTSBackend.EDGE.value,
    "kokoro": TTSBackend.KOKORO.value,
    "qwen3_tts": TTSBackend.QWEN3_TTS.value,
    "zono2": TTSBackend.ZONOS2.value,
    "zonos2": TTSBackend.ZONOS2.value,
    "voxcpm2": TTSBackend.VOXCPM2.value,
    "fish_tts": TTSBackend.FISH.value,
    "openai_tts": TTSBackend.OPENAI.value,
}


def _gateway_engine_values(item: dict, *, category: str) -> list[str]:
    """Tag one gateway catalogue item with the public engine(s) that accept it.

    The gateway now reports the sub-engine that owns each stock voice; clones
    carry a comma-separated engine pin. Legacy payloads without the field keep
    the historical OmniVoice tag.

    Args:
        item: Stock or cloned voice dict from the gateway.
        category: ``stock`` or ``cloned``.

    Returns:
        Canonical ``TTSBackend`` values, in the order the gateway reported them.
    """
    raw = str(item.get("engine") or "").strip()
    if not raw:
        return [TTSBackend.OMNIVOICE.value]
    values: list[str] = []
    for part in raw.split(","):
        name = part.strip().lower()
        if not name:
            continue
        values.append(_GATEWAY_ENGINE_VALUES.get(name, TTSBackend.GATEWAY.value))
    return list(dict.fromkeys(values)) or [TTSBackend.OMNIVOICE.value]


def _dedupe(voices: list[VoiceOption]) -> list[VoiceOption]:
    """Merge entries that repeat an id across sources.

    The same id can legitimately belong to two vocabularies (an edge/gateway
    alias), in which case the union of engines is what both sources mean. The
    metadata (label, language, category) of the first entry seen for an id is
    kept, so source order decides whose origin wins a collision.

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


async def gateway_options(engine: str | None = None) -> tuple[list[str], list[VoiceOption]]:
    """Fetch the tts-api gateway voices (stock + clones).

    Args:
        engine: Optional gateway engine id. When set, its own stock catalogue is
            fetched with ``GET /voices/stock?engine=...``; clones still come from
            the gateway's clone registry and are tagged with their own engine.

    Returns:
        Tuple of ``(languages, voices)``. Both empty when the gateway is
        unreachable, so the page still renders.
    """
    from src.api.services.voices import VoicesService

    service = VoicesService()
    try:
        if engine:
            stock = await service.list_stock(engine=engine)
            languages = sorted({str(v["language"]).strip() for v in stock if v.get("language")})
        else:
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
                    engines=_gateway_engine_values(item, category=category),
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

    custom_names = {profile.name for profile in custom}
    voices: list[VoiceOption] = []
    seen: set[str] = set()
    for profile in [*profiles, *custom]:
        if profile.name in seen:
            continue
        seen.add(profile.name)
        voices.append(
            VoiceOption(
                id=profile.name,
                label=profile.label or profile.name,
                language=profile.language,
                engines=list(MODAL_ENGINES),
                category="generated" if profile.name in custom_names else "stock",
            )
        )
    languages = sorted({voice.language for voice in voices if voice.language})
    return languages, voices


async def list_all_voices(
    engine: str | None = None,
    category: str | None = None,
    *,
    gateway_engine: str | None = None,
) -> list[VoiceOption]:
    """List every selectable voice of every engine.

    Args:
        engine: Optional canonical engine id ('edge', 'omni_voice', ...);
            keeps the voices whose id that engine accepts.
        category: Optional 'stock', 'cloned' or 'generated'.
        gateway_engine: Optional gateway engine id whose stock catalogue must be
            loaded (e.g. ``kokoro``, ``pocket_tts``, ``piper``).

    Returns:
        The merged voices, ordered by category, language and label.
    """
    _, edge = await edge_options()
    _, gateway = (
        await gateway_options(engine=gateway_engine) if gateway_engine else await gateway_options()
    )
    _, modal = modal_options()

    # The Modal catalog is the authoritative metadata for a reference voice: a
    # committed profile keeps its ``stock`` origin (or ``generated`` for an
    # operator-created one) even when the gateway holds a clone of the same
    # name. Ordering modal before gateway lets the catalog's origin and label
    # win the merge, while the clone still contributes its engine.
    voices = _dedupe([*edge, *modal, *gateway])
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


def _accepts_backend(voice: VoiceOption, backend: str) -> bool:
    """Check one voice against the submit guard used by TTS forms (FASE 13).

    Args:
        voice: Catalogue entry under consideration.
        backend: Canonical backend the form would submit.

    Returns:
        True when the guard accepts the id, including unknown ids whose engine
        cannot be inferred from their spelling.
    """
    from src.api.services.voice_engine import engines_for_voice

    allowed = engines_for_voice(voice.id)
    return not allowed or backend in allowed


def _unique_voices(voices: list[VoiceOption]) -> list[VoiceOption]:
    """Return voices in input order with duplicate ids removed.

    Args:
        voices: Voices from one or more sources.

    Returns:
        One entry per id, keeping the first occurrence.
    """
    seen: set[str] = set()
    result: list[VoiceOption] = []
    for voice in voices:
        if voice.id in seen:
            continue
        seen.add(voice.id)
        result.append(voice)
    return result


async def voices_for_engine(
    backend: str | None, gateway_engine: str | None = None
) -> list[VoiceOption]:
    """List the voices selectable with one TTS backend (FASE 13).

    Unlike :func:`list_all_voices`, this helper resolves the operator's backend
    alias and loads only the vocabulary that backend can actually synthesize.
    Gateway sub-engines (``kokoro``, ``piper``, ``qwen3_tts``, ...) are queried
    with their own stock catalogue, so a pin change immediately changes both
    the language and the voice choices.

    Args:
        backend: Canonical backend id or one of its accepted aliases.
        gateway_engine: Gateway sub-engine pin of ``gateway``/``omni_voice``.

    Returns:
        Voices ordered by category, language and label. Sources that cannot be
        reached contribute no voices instead of raising.
    """
    from src.api.services.tts_backends import resolve_backend

    raw = (backend or "").strip().lower()
    unresolved = False
    try:
        engine = resolve_backend(raw).value
        pin = (gateway_engine or "").strip().lower() or _GATEWAY_BACKEND_PINS.get(engine, "")
    except Exception:  # noqa: BLE001 - an unregistered id is a gateway sub-engine
        engine = TTSBackend.GATEWAY.value
        pin = raw
        unresolved = True

    if engine == TTSBackend.EDGE.value:
        _, voices = await edge_options()
        return list(voices)
    if engine in MODAL_ENGINES:
        _, voices = modal_options()
        return list(voices)
    if engine == TTSBackend.FISH.value:
        _, gateway = await gateway_options(engine=pin or "fish_tts")
        _, modal = modal_options()
        fish = [voice for voice in gateway if TTSBackend.FISH.value in voice.engines]
        clones = [voice.model_copy(update={"engines": [TTSBackend.FISH.value]}) for voice in modal]
        return _unique_voices([*fish, *clones])
    if engine == TTSBackend.OPENAI.value:
        _, gateway = await gateway_options(engine=pin or "openai_tts")
        return [voice for voice in gateway if TTSBackend.OPENAI.value in voice.engines]

    if engine == TTSBackend.OMNIVOICE.value and not pin:
        _, voices = await gateway_options()
        return list(voices)
    _, voices = await gateway_options(engine=pin or None)
    if unresolved:
        return list(voices)
    guard = _GATEWAY_ENGINE_VALUES.get(pin, pin) if pin else engine
    if guard and guard not in (TTSBackend.GATEWAY.value, TTSBackend.OMNIVOICE.value):
        voices = [voice for voice in voices if _accepts_backend(voice, guard)]
    return list(voices)


async def languages_for_engine(backend: str | None, gateway_engine: str | None = None) -> list[str]:
    """List the ISO 639-1 languages offered by one TTS backend (FASE 13).

    Args:
        backend: Canonical backend id or one of its accepted aliases.
        gateway_engine: Gateway sub-engine pin of ``gateway``/``omni_voice``.

    Returns:
        Sorted language prefixes derived from that backend's voices.
    """
    voices = await voices_for_engine(backend, gateway_engine)
    return sorted({voice.language.split("-")[0].lower() for voice in voices if voice.language})
