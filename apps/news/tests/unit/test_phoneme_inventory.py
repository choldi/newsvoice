"""Unit tests for the phoneme inventories and catalog coverage (Phase 7.9).

The reference audio a voice is cloned from is only as good as the phonetic
range of the text it reads, so these tests pin two invariants:

* every declared phoneme is actually exercised by a coverage word, and
* every catalog reference transcript contains those words.

The second one is what stops a well-meaning edit from silently dropping the
only word that carried a rare phoneme.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from src.core.phoneme_inventory import (
    coverage_words,
    inventory,
    missing_phonemes,
    words_missing_from,
)
from src.core.voice_catalog import load_profiles

#: The committed catalog, which the gateway ships and serves since P6.
CATALOG = Path(__file__).resolve().parents[3] / "gateway" / "voice_profiles.json"

LANGUAGES = ("en", "es", "fr")


# --- inventory coverage ------------------------------------------------------


def test_english_inventory_is_fully_covered() -> None:
    assert missing_phonemes("en") == set()


def test_spanish_inventory_is_fully_covered() -> None:
    assert missing_phonemes("es") == set()


def test_french_inventory_is_fully_covered() -> None:
    assert missing_phonemes("fr") == set()


def test_coverage_words_only_use_known_phonemes() -> None:
    """A typo in a coverage word's phonemes would silently drop a phoneme."""
    for language in LANGUAGES:
        known = set(inventory(language))
        for word in coverage_words(language):
            unknown = set(word.phonemes) - known
            assert not unknown, f"{language} {word.word!r} has unknown phonemes: {unknown}"


def test_coverage_words_are_unique_per_language() -> None:
    for language in LANGUAGES:
        names = [word.word for word in coverage_words(language)]
        assert len(names) == len(set(names)), f"{language} has duplicate coverage words"


def test_inventories_are_non_empty_and_disjoint_from_nothing() -> None:
    for language in LANGUAGES:
        assert len(inventory(language)) > 20


# --- catalog coverage -------------------------------------------------------


def test_profiles_of_one_language_share_a_ref_text() -> None:
    """ECAPA comparisons are only comparable if the reference text is shared."""
    by_language: dict[str, set[str]] = defaultdict(set)
    for profile in load_profiles(CATALOG):
        by_language[profile.language].add(profile.ref_text)

    for language, ref_texts in by_language.items():
        assert len(ref_texts) == 1, f"{language} has {len(ref_texts)} distinct ref_texts"


def test_repo_catalog_languages_all_have_an_inventory() -> None:
    languages = {profile.language for profile in load_profiles(CATALOG)}
    for language in languages:
        assert language in set(LANGUAGES), f"no phoneme inventory for {language!r}"


def test_repo_catalog_ref_texts_cover_the_inventory() -> None:
    """Every catalog transcript contains every coverage word of its language.

    The inventory check alone only proves the curated word list is complete;
    this is the one that fails when someone edits a transcript and drops the
    only word carrying a rare phoneme.
    """
    by_language: dict[str, set[str]] = defaultdict(set)
    for profile in load_profiles(CATALOG):
        by_language[profile.language].add(profile.ref_text)

    for language, ref_texts in by_language.items():
        for ref_text in ref_texts:
            missing = words_missing_from(language, ref_text)
            assert not missing, f"{language} ref_text is missing: {missing}"
