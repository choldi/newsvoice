"""Unit tests for the voice search service (Phase 9.6).

The glob contract is the heart of the tools pickers: plain text is a
substring search, ``*``/``?`` behave like shell wildcards and the pattern is
matched anywhere in the haystack, so ``Mich*clo`` finds "Michael_sss
en(glish) Voz clonada".
"""

from __future__ import annotations

from src.api.models.voices import VoiceOption
from src.api.services.voice_search import (
    DEFAULT_PAGE_SIZE,
    filter_voices,
    language_options,
    matches,
    paginate,
    picker_order,
)

CLONED = VoiceOption(
    id="123e4567-e89b-12d3-a456-426614174000",
    label="Michael_sss en(glish) Voz clonada",
    language="es-ES",
    engines=["omni_voice"],
    category="cloned",
)
EDGE = VoiceOption(
    id="es-ES-DarioNeural",
    label="Dario (Neural)",
    language="es-ES",
    engines=["edge"],
    category="stock",
)
EDGE_MX = VoiceOption(
    id="es-MX-JorgeNeural",
    label="Jorge (Neural)",
    language="es-MX",
    engines=["edge"],
    category="stock",
)
DESIGNED = VoiceOption(
    id="mi_voz_es",
    label="Profe propia",
    language="es",
    engines=["qwen3_tts", "zonos2"],
    category="generated",
)
ALL = [EDGE, EDGE_MX, CLONED, DESIGNED]


# ---------------------------------------------------------------------------
# Query semantics
# ---------------------------------------------------------------------------


def test_user_example_glob_matches_a_label_that_neither_starts_nor_ends_with_it() -> None:
    """'Mich*clo' finds 'Michael_sss en(glish) Voz clonada'."""
    assert matches(CLONED, "Mich*clo")


def test_plain_text_is_a_case_insensitive_substring() -> None:
    """Typing 'dario' or 'DARIO' finds the edge voice by label or id."""
    assert matches(EDGE, "dario")
    assert matches(EDGE, "DARIO")
    assert matches(EDGE, "es-es-dario")


def test_question_mark_matches_exactly_one_character() -> None:
    """'a?c' stands for 'abc'; two characters have no match on a short id."""
    short = {"id": "abc", "label": "abc", "language": "", "engines": [], "category": "stock"}
    assert matches(short, "a?c")
    assert not matches(short, "a??c")
    assert matches(EDGE, "?ario")


def test_regex_metacharacters_in_the_query_are_literal() -> None:
    """A stray '(', '[' or '+' never breaks the search and means itself."""
    assert matches(EDGE, "ario (")
    assert not matches(EDGE, "[dario")
    assert not matches(EDGE, "dario+")


def test_empty_query_matches_everything() -> None:
    """The unfiltered listing shows every voice."""
    assert all(matches(voice, "") for voice in ALL)


def test_query_covers_id_label_language_category_and_engines() -> None:
    """Searching 'cloned', 'generated', 'qwen3_tts' or 'es-mx' narrows down."""
    assert filter_voices(ALL, query="cloned") == [CLONED]
    assert filter_voices(ALL, query="generated") == [DESIGNED]
    assert filter_voices(ALL, query="qwen3_tts") == [DESIGNED]
    assert filter_voices(ALL, query="es-mx") == [EDGE_MX]


# ---------------------------------------------------------------------------
# Structured filters
# ---------------------------------------------------------------------------


def test_language_filter_matches_by_prefix() -> None:
    """'es' matches es-ES, es-MX and es; 'es-ES' matches only es-ES."""
    assert filter_voices(ALL, lang="es") == ALL
    assert filter_voices(ALL, lang="es-ES") == [EDGE, CLONED]
    assert filter_voices(ALL, lang="en") == []


def test_category_and_engine_filters_combine_with_the_query() -> None:
    """Criteria are ANDed: category stock + engine edge + text."""
    assert filter_voices(ALL, category="stock", engine="edge") == [EDGE, EDGE_MX]
    assert filter_voices(ALL, category="stock", engine="edge", query="jorge") == [EDGE_MX]
    assert filter_voices(ALL, category="cloned", engine="edge") == []


def test_dict_voices_are_supported() -> None:
    """Templates pass plain dicts; the filters accept them unchanged."""
    entries = [voice.model_dump() for voice in ALL]
    assert filter_voices(entries, query="michael") == [entries[2]]


def test_language_options_fold_locales_into_iso_prefixes() -> None:
    """es-ES and es-MX share one 'es' bucket with a correct count."""
    assert language_options(ALL) == [
        {"code": "es", "count": 4},
    ]
    assert language_options([]) == []


# ---------------------------------------------------------------------------
# Picker ordering
# ---------------------------------------------------------------------------


def test_picker_order_puts_designs_and_clones_first() -> None:
    """The unfiltered picker leads with the operator's own voices."""
    ordered = picker_order(list(ALL))
    assert ordered[0] is DESIGNED
    assert ordered[1] is CLONED
    assert ordered[2:] == [EDGE, EDGE_MX]


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_paginate_slices_and_counts() -> None:
    """Page 2 of 4-per-page starts at index 4 and reports the totals."""
    page = paginate(list(range(10)), page=2, size=4)
    assert page.items == [4, 5, 6, 7]
    assert page.page == 2
    assert page.total == 10
    assert page.total_pages == 3
    assert page.has_prev and page.has_next


def test_paginate_clamps_out_of_range_pages_and_sizes() -> None:
    """A crafted page or size can neither error nor escape the range."""
    assert paginate([], page=3, size=25).page == 1
    assert paginate(list(range(10)), page=99, size=4).page == 3
    assert paginate(list(range(10)), page=-5, size=4).page == 1
    assert paginate(list(range(10)), page=1, size=0).size == 1
    assert paginate(list(range(10)), page=1, size="nonsense").size == DEFAULT_PAGE_SIZE
    assert paginate(list(range(300)), page=1, size=10_000).size == 200
    assert paginate([], page=1, size=25).total_pages == 1
