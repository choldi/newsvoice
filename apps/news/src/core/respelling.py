"""Phonetic respelling of foreign terms before text-to-speech (Phase 7.15).

A Spanish news script is full of English brand names and tech terms. Read
verbatim by a Spanish voice, "startup" or "machine learning" come out wrong.
This module asks the local LLM for a JSON map ``{term: respelling}`` and does
the substitution in code: the LLM never rewrites the script, so the Spanish
text, the punctuation and the numbers stay byte-identical. Only the
pronunciation changes.

The result is cached per segment in ``NarrationSegment.tts_text``; ``text``
remains the canonical, human-readable script.
"""

from __future__ import annotations

import json
import re

import structlog

from src.api.models.narration import NarrationScript, NarrationSegment, NarrationSpec, TTSBackend
from src.core.config import get_settings
from src.core.llm import generate_completion
from src.core.prompts import strip_emotion_tags

logger = structlog.get_logger()

#: Shorter tokens are either native words or acronyms already handled by case.
MIN_TOKEN_LENGTH = 3
#: Bound the candidate hint sent to the LLM; a longer list rarely fits a prompt.
MAX_CANDIDATES = 60

_LANGUAGE_NAMES = {
    "es": "Spanish",
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


def is_candidate(token: str, language: str = "es") -> bool:
    """Return True when a token looks like it needs a respelling.

    The heuristic is deliberately broad: it only decides whether to ask the
    LLM, and the LLM drops the terms that are already native. English-shaped
    tokens are flagged for every target language.

    Args:
        token: Word extracted from the script, possibly with punctuation.
        language: Target ISO 639-1 code.

    Returns:
        True if the token may be foreign or mispronounced.
    """
    raw = token.strip(".,;:!?()[]{}\"'“”‘’«»…")
    if len(raw) < 2:
        return False
    lower = raw.lower()
    native = _NATIVE_WORDS.get(language, frozenset())
    if lower in native:
        return False
    # Mixed letter+digit tokens (M4, 5G, GPT-4) are always worth a look.
    if any(char.isdigit() for char in raw) and any(char.isalpha() for char in raw):
        return True
    if not raw.isascii():
        return False
    if raw.isupper() and len(raw) >= 2:
        return True
    if lower in _TECH_TERMS:
        return True
    # CamelCase or internal capital (iPhone, GitHub, OpenAI).
    if raw != raw.lower() and raw != raw.capitalize():
        return True
    if len(lower) < MIN_TOKEN_LENGTH:
        return False
    if not raw.replace("-", "").replace("'", "").replace("’", "").isalpha():
        return False
    if any(lower.endswith(suffix) for suffix in _ENGLISH_SUFFIXES):
        return True
    if any(digraph in lower for digraph in _ENGLISH_DIGRAPHS):
        return True
    # Spanish does not start words with these clusters.
    return bool(re.match(r"^[a-z]*(st|sp|sc|sk)[a-z]*$", lower))


def find_candidates(text: str, language: str = "es") -> list[str]:
    """Return the distinct tokens of ``text`` worth asking the LLM about.

    Args:
        text: Text to scan.
        language: Target ISO 639-1 code.

    Returns:
        Candidate terms in order of first appearance, capped at
        ``MAX_CANDIDATES``.
    """
    candidates: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN_RE.findall(text):
        if not is_candidate(token, language):
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(token)
        if len(candidates) >= MAX_CANDIDATES:
            break
    return candidates


def _examples_for(language: str) -> str:
    """Format the few-shot examples for the target language.

    Args:
        language: Target ISO 639-1 code.

    Returns:
        One example per line, or an empty string when the language has none.
    """
    examples = _EXAMPLES.get(language, ())
    return "\n".join(f'  "{term}" -> "{respelling}"' for term, respelling in examples)


def _system_prompt(language: str) -> str:
    """Build the system prompt for one target language.

    Args:
        language: Target ISO 639-1 code.

    Returns:
        The prompt asking for a JSON ``{term: respelling}`` map.
    """
    name = _LANGUAGE_NAMES[language]
    examples = _examples_for(language)
    examples_block = f"\nExamples in {name}:\n{examples}\n" if examples else ""
    return (
        "You are a phonetic respelling engine for a text-to-speech system.\n"
        f"The input is text that will be read aloud in {name} ({language}).\n"
        "Return a single JSON object. Each key is an exact term from the input; "
        "each value is how that term must be written so that a native "
        f"{name} speaker and a {name} TTS voice pronounce it the way it sounds "
        "in its source language.\n"
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


async def respell_text(text: str, language: str = "es") -> str:
    """Return ``text`` with its foreign terms respelled for ``language``.

    Args:
        text: Segment text, already free of emotion tags.
        language: Target ISO 639-1 code.

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
    if code not in _LANGUAGE_NAMES:
        logger.warning("respelling_unsupported_language", language=code)
        return text
    candidates = find_candidates(text, code)
    if not candidates:
        logger.debug("respelling_no_candidates", language=code)
        return text
    raw = await generate_completion(
        messages=[
            {"role": "system", "content": _system_prompt(code)},
            {"role": "user", "content": _user_prompt(text, candidates)},
        ],
        temperature=settings.respelling_temperature,
        response_format="json_object",
        disable_thinking=True,
    )
    mapping = parse_respelling_json(raw)
    respelled = apply_respelling(text, mapping)
    logger.info(
        "respelling_done",
        language=code,
        candidates=len(candidates),
        terms=len(mapping),
        changed=respelled != text,
    )
    return respelled


async def respell_script(spec: NarrationSpec, script: NarrationScript) -> NarrationScript:
    """Fill ``tts_text`` on every segment of a QWEN3_TTS script.

    Segments that already carry ``tts_text`` are left untouched: that field is
    the cache, so a re-render never pays for the same LLM call twice. A failed
    call leaves ``tts_text`` unset and the renderer falls back to ``text``.

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
        try:
            respelled = await respell_text(text, language)
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
