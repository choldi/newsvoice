"""Wildcard search, filters and pagination over the voice registry (Phase 9.6).

The GUI lists every selectable voice (the Edge catalogue alone has ~700
entries, plus gateway stock, clones and Modal profiles), so browsing and
picking need more than a long ``<select>``. This module provides the shared
query semantics used by the ``/ui/voices`` table and by the tools voice
pickers:

* a glob-style query — ``Mich*clo`` matches "Michael_sss en(glish) Voz
  clonada" — applied case-insensitively and **unanchored** (plain text is a
  substring search, ``*`` matches any run of characters, ``?`` exactly one);
* structured filters: language prefix (``es`` matches ``es`` and ``es-ES``),
  category and engine;
* page slicing with clamped page numbers.

Both functions accept registry :class:`~src.api.models.voices.VoiceOption`
objects and the plain ``{'id', 'label', 'language', 'engines', 'category'}``
dicts the templates already pass around, so callers never have to normalize.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from math import ceil

#: Display order of the categories inside the pickers: the operator's own
#: voices (clones and designs) are what they look for first, so they lead the
#: unfiltered list while the bulk of edge stock sinks to the bottom.
PICKER_CATEGORY_RANK: dict[str, int] = {"generated": 0, "cloned": 1, "stock": 2}

#: Default and bounds of the table page size.
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200


def _field(voice: object, key: str) -> object:
    """Read one registry field from a VoiceOption or a template dict.

    Args:
        voice: A :class:`VoiceOption` or a plain dict with registry keys.
        key: Field name ('id', 'label', 'language', 'engines', 'category').

    Returns:
        The field value, or '' when absent.
    """
    if isinstance(voice, dict):
        return voice.get(key, "")
    return getattr(voice, key, "")


def _compile_query(query: str) -> re.Pattern[str] | None:
    """Translate a glob-style query into an unanchored, case-folded regex.

    Every literal character is escaped, so any input compiles: ``*`` becomes
    ``.*``, ``?`` becomes ``.`` and the result is searched anywhere in the
    haystack (the user's ``Mich*clo`` example matches a label that neither
    starts nor ends with the pattern).

    Args:
        query: Raw query from the filter field.

    Returns:
        The compiled pattern, or None for an empty query (matches everything).
    """
    text = query.strip().casefold()
    if not text:
        return None
    parts: list[str] = []
    for char in text:
        if char == "*":
            parts.append(".*")
        elif char == "?":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    return re.compile("".join(parts))


def _haystack(voice: object) -> str:
    """Build the case-folded text a query is matched against.

    Args:
        voice: A VoiceOption or registry dict.

    Returns:
        id, label, language, category and engines joined by spaces.
    """
    engines = _field(voice, "engines") or []
    return " ".join(
        str(part)
        for part in (
            _field(voice, "id"),
            _field(voice, "label"),
            _field(voice, "language"),
            _field(voice, "category"),
            " ".join(str(engine) for engine in engines),
        )
        if part
    ).casefold()


def matches(voice: object, query: str) -> bool:
    """Tell whether one voice satisfies a glob-style query.

    Args:
        voice: A VoiceOption or registry dict.
        query: Glob pattern; empty matches everything.

    Returns:
        True when the pattern is found in the voice's id, label, language,
        category or engines (case-insensitively).
    """
    pattern = _compile_query(query)
    return pattern is None or pattern.search(_haystack(voice)) is not None


def _engines(voice: object) -> list:
    """Return the engine list of a voice, tolerating dict or model input.

    Args:
        voice: A VoiceOption or registry dict.

    Returns:
        The engines list, empty when absent.
    """
    engines = _field(voice, "engines")
    return list(engines) if engines else []


def filter_voices(
    voices: list,
    *,
    query: str = "",
    lang: str = "",
    category: str = "",
    engine: str = "",
) -> list:
    """Filter voices by glob query, language prefix, category and engine.

    Args:
        voices: VoiceOption objects or registry dicts.
        query: Glob-style pattern (see :func:`matches`); '' matches all.
        lang: Language prefix: 'es' matches 'es' and 'es-ES', 'es-ES'
            matches only 'es-ES'. '' matches all.
        category: Exact category ('stock', 'cloned', 'generated'); '' all.
        engine: Canonical engine id the voice must accept; '' all.

    Returns:
        The voices matching every non-empty criterion, input order kept.
    """
    pattern = _compile_query(query)
    lang_prefix = lang.strip().casefold()
    wanted_category = category.strip().casefold()
    wanted_engine = engine.strip().casefold()
    result = []
    for voice in voices:
        if pattern is not None and pattern.search(_haystack(voice)) is None:
            continue
        if lang_prefix:
            voice_lang = str(_field(voice, "language") or "").casefold()
            if not voice_lang.startswith(lang_prefix):
                continue
        if wanted_category and str(_field(voice, "category") or "").casefold() != wanted_category:
            continue
        if wanted_engine and wanted_engine not in [str(e).casefold() for e in _engines(voice)]:
            continue
        result.append(voice)
    return result


def picker_order(voices: list) -> list:
    """Sort voices for the pickers: designs and clones before the stock bulk.

    Args:
        voices: Filtered VoiceOption objects or registry dicts.

    Returns:
        A new list ordered by category (generated, cloned, stock), then
        language and label.
    """
    return sorted(
        voices,
        key=lambda voice: (
            PICKER_CATEGORY_RANK.get(str(_field(voice, "category") or ""), 9),
            str(_field(voice, "language") or ""),
            str(_field(voice, "label") or "").casefold(),
        ),
    )


def language_options(voices: list) -> list[dict]:
    """Build the language filter options with their match counts.

    Locale spellings are folded to their ISO 639-1 prefix, so edge's
    ``es-ES``/``es-MX`` and the gateway's ``es`` share one ``es`` option.

    Args:
        voices: VoiceOption objects or registry dicts.

    Returns:
        One ``{'code', 'count'}`` dict per language prefix, sorted by code.
    """
    counts: dict[str, int] = {}
    for voice in voices:
        code = str(_field(voice, "language") or "").split("-")[0].strip().casefold()
        if code:
            counts[code] = counts.get(code, 0) + 1
    return [{"code": code, "count": counts[code]} for code in sorted(counts)]


@dataclass(frozen=True)
class Page:
    """One page of a filtered voice listing.

    Attributes:
        items: The voices of this page.
        page: 1-based page number, already clamped to the valid range.
        size: Page size.
        total: Number of items across every page.
    """

    items: list
    page: int
    size: int
    total: int

    @property
    def total_pages(self) -> int:
        """Return the number of pages (at least one, even when empty).

        Returns:
            ceil(total / size), minimum 1.
        """
        return max(1, ceil(self.total / self.size))

    @property
    def has_prev(self) -> bool:
        """Tell whether a previous page exists.

        Returns:
            True when page > 1.
        """
        return self.page > 1

    @property
    def has_next(self) -> bool:
        """Tell whether a next page exists.

        Returns:
            True when page < total_pages.
        """
        return self.page < self.total_pages


def paginate(voices: list, page: int | str = 1, size: int | str = DEFAULT_PAGE_SIZE) -> Page:
    """Slice a filtered voice list into a clamped page.

    Args:
        voices: The full filtered list.
        page: Requested 1-based page (coercible to int); clamped into
            ``[1, total_pages]``.
        size: Page size (coercible to int); clamped into ``[1, MAX_PAGE_SIZE]``.

    Returns:
        The :class:`Page` with the requested slice.
    """
    try:
        size = int(size)
    except (TypeError, ValueError):
        size = DEFAULT_PAGE_SIZE
    size = min(max(size, 1), MAX_PAGE_SIZE)
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    total = len(voices)
    total_pages = max(1, ceil(total / size))
    page = min(max(page, 1), total_pages)
    start = (page - 1) * size
    return Page(items=voices[start : start + size], page=page, size=size, total=total)
