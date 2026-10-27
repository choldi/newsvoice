"""Phonetic respelling of foreign terms before text-to-speech (Phase 7.15).

A Spanish news script is full of English brand names and tech terms. Read
verbatim by a Spanish voice, "startup" or "machine learning" come out wrong.
This module asks the local LLM for a JSON map ``{term: respelling}`` and does
the substitution in code: the LLM never rewrites the script, so the Spanish
text, the punctuation and the numbers stay byte-identical. Only the
pronunciation changes.

The result is cached per segment in ``NarrationSegment.tts_text``; ``text``
remains the canonical, human-readable script.

How a candidate is recognised is a **language pair**: terms of
``language_origin`` (what the foreign words are) read inside text of
``language_target`` (who reads them). Phase 7.16 moved those rules —
suffixes, digraphs, tech terms, native-word stop list, few-shot examples —
into the ``phonetic_heuristics`` table (migration 015, editable at
``/ui/phonetics``); the Phase 7.15 English-in-Spanish constants survive here
as the built-in fallback for a pair nobody configured, so narration keeps
working on a database without rows.

Two callers, two contracts:

* :func:`respell_script` (narration, Phase 7.15): best effort — a failing
  LLM call leaves the segment untouched.
* :func:`respell_lesson_script` (teacher, Phase 7.16): strict — it raises
  :class:`HeuristicsError` when the pair is unconfigured and lets the LLM
  error propagate, because a lesson must never ship half-adapted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import structlog

from src.api.models.narration import NarrationScript, NarrationSegment, NarrationSpec, TTSBackend
from src.api.models.phonetic import PhoneticHeuristicsOut
from src.api.services import phonetic_heuristics as heuristics_service
from src.api.services.phonetic_heuristics import HeuristicsError
from src.core.config import get_settings
from src.core.llm import generate_completion
from src.core.prompts import strip_emotion_tags

logger = structlog.get_logger()

#: Shorter tokens are either native words or acronyms already handled by case.
MIN_TOKEN_LENGTH = 3
#: Bound the candidate hint sent to the LLM; a longer list rarely fits a prompt.
MAX_CANDIDATES = 60
#: Assumed source of the foreign terms when a caller (narration) does not say:
#: Phase 7.15 only ever hunted English terms.
DEFAULT_LANGUAGE_ORIGIN = "en"

_LANGUAGE_NAMES = {
    "es": "Spanish",
    "ca": "Catalan",
    "fr": "French",
    "en": "English",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
}

#: Brands and tech terms that the shape heuristic alone would miss.
_TECH_TERMS = frozenset(
    {
        "startup",
        "machine",
        "learning",
        "kernel",
        "streaming",
        "chip",
        "cloud",
        "software",
        "hardware",
        "firmware",
        "gadget",
        "benchmark",
        "blockchain",
        "funding",
        "dataset",
        "prompt",
        "token",
        "cluster",
        "server",
        "gateway",
        "framework",
        "runtime",
        "library",
        "release",
        "update",
        "patch",
        "deploy",
        "pipeline",
        "workflow",
        "opensource",
        "ai",
        "llm",
        "gpu",
        "cpu",
        "api",
        "sdk",
        "apple",
        "google",
        "microsoft",
        "amazon",
        "meta",
        "nvidia",
        "openai",
        "github",
        "iphone",
        "ipad",
        "macos",
        "windows",
        "android",
        "linux",
        "python",
        "javascript",
        "typescript",
        "rust",
        "docker",
        "kubernetes",
    }
)

#: Endings that almost never appear in a native Spanish word.
_ENGLISH_SUFFIXES = (
    "ing",
    "tion",
    "sion",
    "ment",
    "ness",
    "able",
    "ible",
    "ical",
    "ware",
    "work",
    "book",
    "mail",
    "site",
    "chat",
    "bot",
    "tech",
)

#: Digraphs that flag an English-looking token in Spanish.
_ENGLISH_DIGRAPHS = ("th", "sh", "ch", "ck", "wh", "ph", "gh", "ou", "oo", "ea")

#: Native words that the shape heuristic would otherwise flag. Keeping them
#: out of the hint list costs nothing and prevents needless LLM calls.
_NATIVE_WORDS = {
    "es": frozenset(
        {
            "el",
            "la",
            "de",
            "que",
            "un",
            "una",
            "para",
            "con",
            "los",
            "las",
            "del",
            "por",
            "como",
            "pero",
            "entre",
            "desde",
            "hasta",
            "sobre",
            "sin",
            "muy",
            "hay",
            "son",
            "fue",
            "ser",
            "tiene",
            "puede",
            "hace",
            "todo",
            "todos",
            "otra",
            "otro",
            "nuestro",
            "nuestra",
            "tambien",
            "también",
            "ademas",
            "además",
            "donde",
            "cuando",
            "porque",
            "mientras",
            "segun",
            "según",
            "tras",
            "cada",
            "esto",
            "esta",
            "este",
            "estos",
            "estas",
            "eso",
            "esa",
            "es",
            "en",
            "y",
            "o",
            "a",
            "su",
            "sus",
            "al",
            "lo",
            "le",
            "les",
            "se",
            "ya",
            "no",
            "si",
            "sí",
            "más",
            "mas",
            "sus",
            "hoy",
            "ayer",
            "mañana",
            "ano",
            "año",
            "años",
            "dia",
            "día",
            "días",
            "vez",
            "veces",
            "gente",
            "empresa",
            "empresas",
            "tecnologia",
            "tecnología",
            "datos",
            "usuario",
            "usuarios",
            "sistema",
            "sistemas",
            "servicio",
            "servicios",
            "producto",
            "productos",
            "mundo",
            "pais",
            "país",
            "gobierno",
            "nuevo",
            "nueva",
            "gran",
            "mejor",
            "mayor",
            "menor",
            "primer",
            "primera",
            "dos",
            "tres",
        }
    ),
    "fr": frozenset(
        {
            "le",
            "la",
            "les",
            "de",
            "des",
            "du",
            "un",
            "une",
            "et",
            "en",
            "est",
            "que",
            "qui",
            "pour",
            "avec",
            "dans",
            "sur",
            "pas",
            "plus",
            "au",
            "aux",
            "ce",
            "cette",
            "ces",
            "son",
            "sa",
            "ses",
            "nous",
            "vous",
            "ils",
            "elles",
            "mais",
            "comme",
            "tout",
            "tous",
            "toute",
            "toutes",
            "jour",
            "jours",
            "année",
            "années",
            "monde",
            "entreprise",
            "entreprises",
            "données",
            "système",
            "systèmes",
            "service",
            "services",
            "produit",
            "produits",
        }
    ),
}

_TOKEN_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

_EXAMPLES = {
    "es": (
        ("startup", "estartap"),
        ("machine learning", "mashin lerning"),
        ("streaming", "estriming"),
        ("cloud", "claud"),
        ("blockchain", "bloqeinin"),
        ("Apple", "apel"),
    ),
    "fr": (
        ("startup", "startap"),
        ("streaming", "striming"),
        ("cloud", "claoud"),
        ("machine learning", "machine leurninng"),
    ),
    "en": (
        ("Iker", "Eeker"),
        ("Barcelona", "Bar-suh-loh-nuh"),
    ),
}


@dataclass(frozen=True)
class PhoneticHeuristics:
    """How to spot terms of one origin language inside a text read in another.

    Phase 7.16: one instance per ``(language_origin, language_target)`` pair,
    either loaded from the ``phonetic_heuristics`` table or rebuilt from the
    Phase 7.15 constants in this module (``source`` says which).

    Attributes:
        language_origin: ISO 639-1 language the foreign terms come from.
        language_target: ISO 639-1 language the text is read in.
        language_origin_name: Origin language name for the prompt.
        language_target_name: Read-language name for the prompt.
        suffixes: Word endings that flag an origin-shaped token.
        digraphs: Letter pairs that flag an origin-shaped token anywhere.
        tech_terms: Brand/tech words flagged verbatim.
        native_words: Stop list: never offered to the LLM.
        examples: Few-shot ``{term: respelling}`` pairs for the prompt.
        ascii_only: Only consider ASCII tokens (English has no accents).
        flag_all_foreign: Offer every non-native word to the LLM instead of
            relying on the shape heuristic (short foreign words such as
            "merci" have no distinctive ending).
        source: Where the row came from: 'db' or 'builtin'.
    """

    language_origin: str
    language_target: str
    language_origin_name: str
    language_target_name: str
    suffixes: tuple[str, ...] = ()
    digraphs: tuple[str, ...] = ()
    tech_terms: frozenset[str] = frozenset()
    native_words: frozenset[str] = frozenset()
    examples: tuple[tuple[str, str], ...] = ()
    ascii_only: bool = True
    flag_all_foreign: bool = False
    source: str = "builtin"


def builtin_heuristics(language_origin: str, language_target: str) -> PhoneticHeuristics | None:
    """Return the Phase 7.15 defaults for a pair, or None when there are none.

    The constants of this module describe English terms read in a language
    :data:`_LANGUAGE_NAMES` knows; any other origin (French terms in a Spanish
    class, ...) must come from the ``phonetic_heuristics`` table. An unknown
    target returns None so narration does not start respelling languages it
    never supported.

    Args:
        language_origin: ISO 639-1 language of the terms.
        language_target: ISO 639-1 language reading them.

    Returns:
        The built-in heuristics, or None when this module has no defaults.
    """
    if language_origin != DEFAULT_LANGUAGE_ORIGIN or language_target not in _LANGUAGE_NAMES:
        return None
    return PhoneticHeuristics(
        language_origin=language_origin,
        language_target=language_target,
        language_origin_name=_LANGUAGE_NAMES[language_origin],
        language_target_name=_LANGUAGE_NAMES[language_target],
        suffixes=_ENGLISH_SUFFIXES,
        digraphs=_ENGLISH_DIGRAPHS,
        tech_terms=frozenset(_TECH_TERMS),
        native_words=_NATIVE_WORDS.get(language_target, frozenset()),
        examples=tuple(_EXAMPLES.get(language_target, ())),
        ascii_only=True,
        flag_all_foreign=False,
        source="builtin",
    )


def _fallback_rules(language: str) -> PhoneticHeuristics:
    """Return English-shaped rules with no target-language knowledge.

    Only :func:`is_candidate` and :func:`find_candidates` reach this: they are
    synchronous helpers that must answer even for a language this module has
    no defaults for, exactly as Phase 7.15 did before the database existed.

    Args:
        language: Target ISO 639-1 code (used for its display name only).

    Returns:
        English origin rules with empty native words and examples.
    """
    return PhoneticHeuristics(
        language_origin=DEFAULT_LANGUAGE_ORIGIN,
        language_target=language,
        language_origin_name=_LANGUAGE_NAMES.get(DEFAULT_LANGUAGE_ORIGIN, DEFAULT_LANGUAGE_ORIGIN),
        language_target_name=_LANGUAGE_NAMES.get(language, language),
        suffixes=_ENGLISH_SUFFIXES,
        digraphs=_ENGLISH_DIGRAPHS,
        tech_terms=frozenset(_TECH_TERMS),
        native_words=frozenset(),
        examples=(),
        ascii_only=True,
        flag_all_foreign=False,
        source="builtin",
    )


def heuristics_from_row(row: PhoneticHeuristicsOut) -> PhoneticHeuristics:
    """Convert a stored heuristics row into the runtime rules object.

    Args:
        row: Validated row from :mod:`src.api.services.phonetic_heuristics`.

    Returns:
        The equivalent heuristics object (``source='db'``).
    """
    return PhoneticHeuristics(
        language_origin=row.language_origin,
        language_target=row.language_target,
        language_origin_name=row.prompt_origin_name(),
        language_target_name=row.prompt_target_name(),
        suffixes=tuple(row.suffixes),
        digraphs=tuple(row.digraphs),
        tech_terms=frozenset(row.tech_terms),
        native_words=frozenset(row.native_words),
        examples=tuple(row.examples.items()),
        ascii_only=row.ascii_only,
        flag_all_foreign=row.flag_all_foreign,
        source="db",
    )


async def load_heuristics(language_origin: str, language_target: str) -> PhoneticHeuristics | None:
    """Return the heuristics of a pair: database row first, built-in fallback.

    A row that exists but is **disabled** wins over the built-in defaults:
    disabling a pair in the GUI turns its respelling off for good. A database
    problem is logged and also degrades to the built-in rules, because the
    narration render must keep working on an unreadable table.

    Args:
        language_origin: ISO 639-1 language of the terms.
        language_target: ISO 639-1 language reading them.

    Returns:
        The rules to use, or None when the pair is disabled or unknown.
    """
    try:
        row = await heuristics_service.get_pair(language_origin, language_target)
    except Exception as exc:
        logger.warning(
            "phonetic_heuristics_load_failed",
            origin=language_origin,
            target=language_target,
            error=str(exc),
        )
        row = None
    if row is not None:
        return heuristics_from_row(row) if row.enabled else None
    return builtin_heuristics(language_origin, language_target)


async def require_heuristics(language_origin: str, language_target: str) -> PhoneticHeuristics:
    """Return the **enabled** heuristics of a pair from the database (7.16).

    Unlike :func:`load_heuristics` there is no built-in fallback: the lesson
    adaptation must run on rules the operator can see and edit, so an
    unconfigured pair is an error instead of a silent guess.

    Args:
        language_origin: ISO 639-1 language of the terms.
        language_target: ISO 639-1 language reading them.

    Returns:
        The stored rules.

    Raises:
        HeuristicsError: When the pair is missing, disabled or unreadable.
    """
    try:
        row = await heuristics_service.require_enabled_pair(language_origin, language_target)
    except HeuristicsError:
        raise
    except Exception as exc:
        raise HeuristicsError(
            f"cannot read phonetic heuristics for {language_origin} -> {language_target}: {exc}"
        ) from exc
    return heuristics_from_row(row)


def _rules_for(language: str, heuristics: PhoneticHeuristics | None) -> PhoneticHeuristics:
    """Resolve the rules a synchronous candidate check must apply.

    Args:
        language: Target ISO 639-1 code (fallback when no rules are given).
        heuristics: Rules supplied by the caller, or None.

    Returns:
        The given rules, the built-in pair defaults, or English fallback rules.
    """
    if heuristics is not None:
        return heuristics
    return builtin_heuristics(DEFAULT_LANGUAGE_ORIGIN, language) or _fallback_rules(language)


def is_candidate(
    token: str, language: str = "es", *, heuristics: PhoneticHeuristics | None = None
) -> bool:
    """Return True when a token looks like it needs a respelling.

    The heuristic is deliberately broad: it only decides whether to ask the
    LLM, and the LLM drops the terms that are already native. Which shapes
    count as foreign belongs to the language pair (see :class:`PhoneticHeuristics`).

    Args:
        token: Word extracted from the script, possibly with punctuation.
        language: Target ISO 639-1 code (ignored when ``heuristics`` is given).
        heuristics: Rules of the pair being checked, or None for the defaults.

    Returns:
        True if the token may be foreign or mispronounced.
    """
    rules = _rules_for(language, heuristics)
    raw = token.strip(".,;:!?()[]{}\"'“”‘’«»…")
    if len(raw) < 2:
        return False
    lower = raw.lower()
    if lower in rules.native_words:
        return False
    # Mixed letter+digit tokens (M4, 5G, GPT-4) are always worth a look.
    if any(char.isdigit() for char in raw) and any(char.isalpha() for char in raw):
        return True
    if rules.ascii_only and not raw.isascii():
        return False
    if raw.isupper() and len(raw) >= 2:
        return True
    if lower in rules.tech_terms:
        return True
    # CamelCase or internal capital (iPhone, GitHub, OpenAI).
    if raw != raw.lower() and raw != raw.capitalize():
        return True
    if len(lower) < MIN_TOKEN_LENGTH:
        return False
    if not raw.replace("-", "").replace("'", "").replace("’", "").isalpha():
        return False
    if rules.flag_all_foreign:
        return True
    if any(lower.endswith(suffix) for suffix in rules.suffixes):
        return True
    if any(digraph in lower for digraph in rules.digraphs):
        return True
    # Spanish does not start words with these clusters.
    return bool(re.match(r"^[a-z]*(st|sp|sc|sk)[a-z]*$", lower))


def find_candidates(
    text: str, language: str = "es", *, heuristics: PhoneticHeuristics | None = None
) -> list[str]:
    """Return the distinct tokens of ``text`` worth asking the LLM about.

    Args:
        text: Text to scan.
        language: Target ISO 639-1 code (ignored when ``heuristics`` is given).
        heuristics: Rules of the pair, or None for the built-in defaults.

    Returns:
        Candidate terms in order of first appearance, capped at
        ``MAX_CANDIDATES``.
    """
    rules = _rules_for(language, heuristics)
    candidates: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN_RE.findall(text):
        if not is_candidate(token, rules.language_target, heuristics=rules):
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(token)
        if len(candidates) >= MAX_CANDIDATES:
            break
    return candidates


def _examples_for(rules: PhoneticHeuristics) -> str:
    """Format the few-shot examples of one pair.

    Args:
        rules: Heuristics carrying the ``{term: respelling}`` examples.

    Returns:
        One example per line, or an empty string when the pair has none.
    """
    return "\n".join(f'  "{term}" -> "{respelling}"' for term, respelling in rules.examples)


def _system_prompt(rules: PhoneticHeuristics) -> str:
    """Build the system prompt for one language pair.

    Args:
        rules: Heuristics of the pair (names drive the wording).

    Returns:
        The prompt asking for a JSON ``{term: respelling}`` map.
    """
    name = rules.language_target_name
    origin_name = rules.language_origin_name
    examples = _examples_for(rules)
    examples_block = f"\nExamples in {name}:\n{examples}\n" if examples else ""
    return (
        "You are a phonetic respelling engine for a text-to-speech system.\n"
        f"The input is text that will be read aloud in {name} ({rules.language_target}).\n"
        "Return a single JSON object. Each key is an exact term from the input; "
        "each value is how that term must be written so that a native "
        f"{name} speaker and a {name} TTS voice pronounce it the way it sounds "
        f"in {origin_name}.\n"
        "Include foreign words, brand names, product names, acronyms, units "
        "and any token that would be mispronounced if read as "
        f"{name}. Do not translate, do not rephrase and do not touch native "
        f"{name} words. Multi-word terms stay one key "
        '("machine learning").\n'
        "Keep keys exactly as they appear (same casing). Omit any term that "
        "needs no change. If nothing needs respelling, return {}.\n"
        "Output only the JSON object: no markdown, no explanation.\n"
        f"{examples_block}"
    )


def _user_prompt(text: str, candidates: list[str]) -> str:
    """Build the user message with the text and its candidate hints.

    Args:
        text: Original segment text.
        candidates: Terms found by :func:`find_candidates`.

    Returns:
        The user prompt.
    """
    hints = ", ".join(candidates)
    return (
        f"Candidate terms (not exhaustive; add any other term you find):\n{hints}\n\nText:\n{text}"
    )


def parse_respelling_json(raw: str) -> dict[str, str]:
    """Parse the LLM answer into a ``{term: respelling}`` mapping.

    Handles plain JSON, fenced JSON and JSON wrapped in prose. Every non-string
    or empty value is dropped instead of failing the render.

    Args:
        raw: Raw LLM response.

    Returns:
        The sanitized mapping, possibly empty.
    """
    text = raw.strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    data: object
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
    if not isinstance(data, dict):
        return {}
    mapping: dict[str, str] = {}
    for term, respelling in data.items():
        if not isinstance(term, str) or not isinstance(respelling, str):
            continue
        term = term.strip()
        respelling = respelling.strip()
        if len(term) < 2 or not respelling:
            continue
        mapping[term] = respelling
    return mapping


def apply_respelling(text: str, mapping: dict[str, str]) -> str:
    """Replace whole occurrences of each term with its respelling in one pass.

    Longest terms win ("machine learning" before "machine"), and each position
    is replaced once, so a respelling can never be re-respelled.

    Args:
        text: Original text.
        mapping: ``{term: respelling}`` map.

    Returns:
        The text with the terms replaced, or the original text when nothing
        applies.
    """
    clean: dict[str, str] = {}
    for term, respelling in mapping.items():
        term = term.strip()
        respelling = respelling.strip()
        if len(term) < 2 or not respelling or term.lower() == respelling.lower():
            continue
        if not re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE):
            continue
        clean[term] = respelling
    if not clean:
        return text
    terms = sorted(clean, key=len, reverse=True)
    pattern = re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(term) for term in terms) + r")(?!\w)",
        re.IGNORECASE,
    )
    by_lower = {term.lower(): clean[term] for term in terms}
    return pattern.sub(lambda match: by_lower.get(match.group(0).lower(), match.group(0)), text)


async def _respell(
    text: str, rules: PhoneticHeuristics, *, temperature: float
) -> tuple[str, dict[str, str]]:
    """Ask the LLM for the term map of one segment and apply it.

    No settings gate lives here: the two public callers decide whether the
    feature is enabled (``respelling_enabled`` for narration,
    ``teacher_respelling_enabled`` for lessons).

    Args:
        text: Segment text, already free of emotion tags.
        rules: Heuristics of the pair reading this text.
        temperature: Sampling temperature for the map.

    Returns:
        Tuple of (respelled text, ``{term: respelling}`` map actually applied).

    Raises:
        RuntimeError: If the LLM call fails or the provider answers garbage
            the JSON parser cannot rescue.
    """
    candidates = find_candidates(text, rules.language_target, heuristics=rules)
    if not candidates:
        logger.debug("respelling_no_candidates", language=rules.language_target)
        return text, {}
    raw = await generate_completion(
        messages=[
            {"role": "system", "content": _system_prompt(rules)},
            {"role": "user", "content": _user_prompt(text, candidates)},
        ],
        temperature=temperature,
        response_format="json_object",
        disable_thinking=True,
    )
    mapping = parse_respelling_json(raw)
    respelled = apply_respelling(text, mapping)
    logger.info(
        "respelling_done",
        origin=rules.language_origin,
        language=rules.language_target,
        candidates=len(candidates),
        terms=len(mapping),
        changed=respelled != text,
    )
    return respelled, mapping


async def respell_text(
    text: str,
    language: str = "es",
    *,
    heuristics: PhoneticHeuristics | None = None,
    temperature: float | None = None,
) -> str:
    """Return ``text`` with its foreign terms respelled for ``language``.

    English-origin terms are assumed (Phase 7.15 callers never say more);
    pass ``heuristics`` to read another pair's rules without a second lookup.

    Args:
        text: Segment text, already free of emotion tags.
        language: Target ISO 639-1 code (the language reading the text).
        heuristics: Rules of the pair, or None to load them for (en, language).
        temperature: Sampling temperature; defaults to ``respelling_temperature``.

    Returns:
        The respelled text, or the original text when there is nothing to do.

    Raises:
        RuntimeError: If the LLM call fails. Callers may fall back to the
            original text.
    """
    settings = get_settings()
    if not settings.respelling_enabled or not text.strip():
        return text
    code = (language or "es").lower()
    rules = (
        heuristics
        if heuristics is not None
        else await load_heuristics(DEFAULT_LANGUAGE_ORIGIN, code)
    )
    if rules is None:
        logger.warning("respelling_unsupported_language", language=code)
        return text
    respelled, _ = await _respell(
        text,
        rules,
        temperature=settings.respelling_temperature if temperature is None else temperature,
    )
    return respelled


async def respell_script(spec: NarrationSpec, script: NarrationScript) -> NarrationScript:
    """Fill ``tts_text`` on every segment of a QWEN3_TTS script.

    Segments that already carry ``tts_text`` are left untouched: that field is
    the cache, so a re-render never pays for the same LLM call twice. A failed
    call leaves ``tts_text`` unset and the renderer falls back to ``text`` —
    narration is best effort by design; the lesson path is not.

    Args:
        spec: Narration spec (language, cast and backend).
        script: Script to enrich.

    Returns:
        The same script when respelling is disabled/not applicable or every
        segment was already cached, else a copy with ``tts_text`` filled.
    """
    settings = get_settings()
    if not settings.respelling_enabled or spec.tts_backend is not TTSBackend.QWEN3_TTS:
        return script
    segments: list[NarrationSegment] = []
    changed = False
    rules_by_language: dict[str, PhoneticHeuristics | None] = {}
    for segment in script.segments:
        if segment.tts_text is not None:
            segments.append(segment)
            continue
        role = spec.role(segment.role)
        language = role.language or spec.language
        text = strip_emotion_tags(segment.text)
        if not text:
            segments.append(segment)
            continue
        if language not in rules_by_language:
            rules_by_language[language] = await load_heuristics(DEFAULT_LANGUAGE_ORIGIN, language)
        rules = rules_by_language[language]
        if rules is None:
            segments.append(segment)
            continue
        try:
            respelled = await respell_text(text, language, heuristics=rules)
        except Exception as exc:
            logger.warning(
                "respelling_failed",
                role=segment.role,
                language=language,
                error=str(exc),
            )
            segments.append(segment)
            continue
        segments.append(segment.model_copy(update={"tts_text": respelled or text}))
        changed = True
    if not changed:
        return script
    return script.model_copy(update={"segments": segments})


def dual_lacks_native_role(spec: NarrationSpec, native_language: str) -> bool:
    """Tell whether a dual cast has no role that speaks the student's language.

    In ``dual`` mode only the native-language role adapts, so a hand-written
    spec that forgets ``language`` on its roles would adapt nothing at all —
    a silent no-op the strict lesson path must refuse instead.

    Args:
        spec: Cast of the lesson.
        native_language: ISO 639-1 language the student thinks in.

    Returns:
        True when neither a role nor the spec default speaks ``native_language``.
    """
    read = (native_language or "").strip().lower()
    if not read:
        return False
    return not any((role.language or spec.language) == read for role in spec.roles)


async def respell_lesson_script(
    script: NarrationScript,
    spec: NarrationSpec,
    *,
    mode: str,
    target_language: str,
    native_language: str,
    temperature: float | None = None,
) -> tuple[NarrationScript, dict[str, Any]]:
    """Adapt the target-language terms a native-language teacher quotes (7.16).

    A class quotes the language being taught inside text spoken in the
    student's native language ("en francés, decimos **bonjur**"), so the terms
    come from ``target_language`` and the text reading them is written for
    ``native_language``: that is the pair this loads from the database.

    In ``dual`` mode only the native-language role adapts (the target-language
    role speaks its own language natively); in ``mixed``/``native`` the single
    teacher explains natively, so every segment adapts.

    Unlike the narration path this is strict: an unconfigured or disabled pair
    and a failing LLM call both raise, because a half-adapted class would
    teach the wrong pronunciation and the job must fail loudly instead.

    Args:
        script: Lesson script to adapt.
        spec: Cast (the role languages decide who adapts in dual mode).
        mode: Lesson mode: 'dual', 'mixed' or 'native'.
        target_language: ISO 639-1 language being taught (terms come from it).
        native_language: ISO 639-1 language the class is spoken in.
        temperature: Sampling temperature; defaults to
            ``teacher_respelling_temperature``.

    Returns:
        Tuple of (copy with ``tts_text`` filled on the adapted segments,
        metadata: the language pair, the union ``{term: respelling}`` map and
        one entry per adapted segment with its own full term map).

    Raises:
        HeuristicsError: When the pair is unconfigured/disabled or the two
            languages are the same.
        RuntimeError: When the LLM call fails.
    """
    origin = (target_language or "").strip().lower()
    read = (native_language or "").strip().lower()
    if not origin or not read:
        raise HeuristicsError("phonetic adaptation needs a target and a native language")
    if origin == read:
        raise HeuristicsError(
            f"phonetic adaptation needs different target and native languages (both are {origin!r})"
        )
    rules = await require_heuristics(origin, read)
    settings = get_settings()
    if temperature is None:
        temperature = settings.teacher_respelling_temperature
    dual = str(mode or "").strip().lower() == "dual"
    if dual and dual_lacks_native_role(spec, read):
        raise HeuristicsError(
            f"the dual cast declares no {read!r}-language role: give the teaching "
            f"role language={read!r} in the spec (or drop the adaptation)"
        )
    segments: list[NarrationSegment] = []
    adapted_terms: dict[str, str] = {}
    adapted_segments: list[dict[str, Any]] = []
    for index, segment in enumerate(script.segments):
        if dual and (spec.role(segment.role).language or spec.language) != read:
            segments.append(segment)
            continue
        if segment.tts_text is not None:
            segments.append(segment)
            continue
        text = strip_emotion_tags(segment.text)
        if not text:
            segments.append(segment)
            continue
        respelled, mapping = await _respell(text, rules, temperature=temperature)
        segments.append(segment.model_copy(update={"tts_text": respelled or text}))
        if mapping:
            adapted_terms.update(mapping)
            adapted_segments.append(
                {
                    "role": segment.role,
                    "segment_index": index,
                    "original_text": segment.text,
                    "respelled_text": respelled or text,
                    "term_map": dict(mapping),
                }
            )
    metadata: dict[str, Any] = {
        "language_origin": rules.language_origin,
        "language_target": rules.language_target,
        "terms": dict(adapted_terms),
        "segments": adapted_segments,
    }
    logger.info(
        "lesson_respelling_done",
        origin=rules.language_origin,
        target=rules.language_target,
        segments=len(adapted_segments),
        terms=len(adapted_terms),
        dual=dual,
    )
    return script.model_copy(update={"segments": segments}), metadata
