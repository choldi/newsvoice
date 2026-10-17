"""Multi-engine voice guard (Phase 9.2).

Every TTS engine speaks its own voice vocabulary: Edge neural ShortNames, the
gateway's stock ids and clone UUIDs, and the local Modal catalog names. A
selection that mixes two vocabularies either fails at synthesis time or (worse)
silently falls back to a different voice, so the input surfaces reject the mix
before anything is stored or queued.

``engines_for_voice`` is synchronous and offline: it pattern-matches the known
id shapes and consults the local catalog (committed ``voice_profiles.json`` plus
operator-created ``custom_profiles.json``). Ids it does not recognize are
treated as compatible with every engine — refusing old data on a heuristic miss
would be a false positive the operator cannot act on, while ``voice_for`` still
validates at synthesis time.

Two overlaps are deliberate and must stay: Edge and the gateway accept each
other's ids (``LEGACY_VOICE_ALIASES`` maps gateway stock ids onto Edge
ShortNames and the gateway itself accepts ShortNames), and the Modal catalog is
served by both qwen3_tts and zonos2. A selection is "mixed" only when no single
engine can voice every voice in it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from src.api.models.narration import TTSBackend
from src.api.services.tts_backends import ALLOWED_BACKENDS, BACKEND_ALIASES

__all__ = [
    "VoiceEngineError",
    "assert_same_engine",
    "assert_voice_engine",
    "engines_for_voice",
    "infer_backend",
]

#: 'es-MX-JorgeNeural' and the lower-cased legacy form 'es-mx-jorgeneural'.
_EDGE_SHORTNAME = re.compile(r"^[a-z]{2,3}-[a-z]{2,4}-[a-z0-9]+neural$", re.IGNORECASE)
#: Gateway stock ids: 'es-mx-male', 'en-gb-female', 'es-mx' is too short and
#: stays unknown (it resolves through the alias tables at synthesis time).
_GATEWAY_STOCK = re.compile(r"^[a-z]{2}-[a-z]{2,4}-[a-z0-9]+(?:-[a-z0-9]+)*$")
#: Cloned voices are stored under their creation UUID.
_CLONE_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

#: When several engines can voice a selection, prefer the one the existing
#: defaults already use: gateway for network voices, edge for digest-only
#: setups, then the Modal backends in catalog order.
_BACKEND_PREFERENCE: tuple[TTSBackend, ...] = (
    TTSBackend.OMNIVOICE,
    TTSBackend.EDGE,
    TTSBackend.QWEN3_TTS,
    TTSBackend.ZONOS2,
    TTSBackend.VOXCPM2,
)


class VoiceEngineError(ValueError):
    """A voice selection targets an engine that cannot voice it.

    Subclasses ``ValueError`` so Pydantic validators surface it as a field
    error (422) and the GUI handlers keep catching it as they catch every
    other value problem.
    """


def _modal_names() -> set[str]:
    """Load the names the Modal engines accept (committed + custom profiles).

    Returns:
        Profile names, or an empty set when the catalog is unreadable (the
        guard must degrade to lenient, not raise, on a missing file).
    """
    from src.core.voice_catalog import (
        VoiceCatalogError,
        load_custom_profiles,
        load_profiles,
    )

    try:
        names = {profile.name for profile in load_profiles()}
        names.update(profile.name for profile in load_custom_profiles())
    except (VoiceCatalogError, OSError):
        return set()
    return names


def engines_for_voice(voice: str) -> set[str]:
    """Classify a voice id against every engine's vocabulary.

    Args:
        voice: Voice id as configured anywhere in the system.

    Returns:
        Canonical engine values (see ``TTSBackend``) that can synthesize this
        voice. Empty when the id is unknown or unset, which the guard treats
        as "compatible with everything".
    """
    name = (voice or "").strip()
    if not name:
        return set()
    engines: set[str] = set()
    if _EDGE_SHORTNAME.match(name):
        # Edge is native; the gateway accepts ShortNames too.
        engines.update((str(TTSBackend.EDGE), str(TTSBackend.OMNIVOICE)))
    if _GATEWAY_STOCK.match(name):
        # Edge maps gateway stock ids through LEGACY_VOICE_ALIASES.
        engines.update((str(TTSBackend.EDGE), str(TTSBackend.OMNIVOICE)))
    if _CLONE_UUID.match(name):
        engines.add(str(TTSBackend.OMNIVOICE))
    if name in _modal_names():
        engines.update((str(TTSBackend.QWEN3_TTS), str(TTSBackend.ZONOS2), str(TTSBackend.VOXCPM2)))
    return engines


def _canonical_engine(engine: str | TTSBackend | None) -> str:
    """Normalize an engine spelling to its canonical value.

    Args:
        engine: Engine value, alias (``gateway``, ``qwen3``, ...) or None.

    Returns:
        The canonical engine id, or '' when nothing usable was configured (the
        caller then skips the check and lets the runtime default answer).
    """
    raw = str(engine or "").strip().lower()
    if not raw:
        return ""
    return str(BACKEND_ALIASES.get(raw, raw))


def assert_voice_engine(
    voice: str, engine: str | TTSBackend | None, *, field: str = "voice"
) -> None:
    """Check that one voice can be synthesized by one engine.

    Args:
        voice: Voice id to check. Unknown ids pass (see module docstring).
        engine: Selected engine; empty or unknown spellings skip the check.
        field: Field name used in the error message.

    Raises:
        VoiceEngineError: When the voice is known to belong to other engines.
    """
    canonical = _canonical_engine(engine)
    if not canonical or canonical not in ALLOWED_BACKENDS:
        # Empty or misspelled engines are somebody else's error to raise
        # (resolve_backend does it at synthesis time with its own message).
        return
    known = engines_for_voice(voice)
    if not known or canonical in known:
        return
    raise VoiceEngineError(
        f"{field} {voice!r} belongs to engine(s) {sorted(known)}, not to "
        f"{canonical!r}: pick a voice offered by {canonical} or switch the engine"
    )


def assert_same_engine(voices: Iterable[str], *, field: str = "voices") -> None:
    """Check that a selection shares at least one engine capable of all voices.

    Unknown voices are skipped (they act as wildcards), so legacy ids never
    make a selection fail; the check only fires when two known-but-disjoint
    vocabularies are combined.

    Args:
        voices: Voice ids to check.
        field: Field name used in the error message.

    Raises:
        VoiceEngineError: When no single engine can voice every known voice.
    """
    known: dict[str, set[str]] = {}
    for voice in voices:
        name = (voice or "").strip()
        if not name:
            continue
        engines = engines_for_voice(name)
        if engines:
            known[name] = engines
    if not known:
        return
    if set.intersection(*known.values()):
        return
    detail = ", ".join(f"{name!r} -> {sorted(engines)}" for name, engines in sorted(known.items()))
    raise VoiceEngineError(f"mixed TTS engines in {field}: {detail}")


def infer_backend(voices: Iterable[str]) -> str:
    """Pick the engine that should synthesize a set of voices.

    Used by the lesson ``resolve_cast`` path, which builds a ``NarrationSpec``
    without an explicit engine: network voices keep the historical gateway
    default, Modal voices select Modal, and a mixed selection is an error.

    Args:
        voices: Voice ids that will speak.

    Returns:
        Canonical engine id, or '' when no voice is classifiable (the caller
        then leaves the spec default untouched).

    Raises:
        VoiceEngineError: When the voices mix disjoint engines.
    """
    assert_same_engine(voices, field="voices")
    known = [engines for voice in voices if voice.strip() and (engines := engines_for_voice(voice))]
    if not known:
        return ""
    common = set.intersection(*known)
    for candidate in _BACKEND_PREFERENCE:
        if str(candidate) in common:
            return str(candidate)
    return sorted(common)[0]
