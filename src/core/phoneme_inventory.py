"""Phoneme inventories and coverage words for voice reference texts.

The reference audio a voice is cloned from is only as good as the phonetic
range of the text it reads. A reference that never exercises /θ/ or /ɲ/ gives
the speaker embedding nothing to work with for those sounds, so the clone
drifts on them. Each language therefore declares:

* the full phoneme inventory the reference must cover, and
* a curated list of words that together hit every phoneme.

``scripts/check_phoneme_coverage.py`` verifies the union of the coverage words
equals the inventory, and that every catalog ``ref_text`` actually contains
those words, so the two can never drift apart.

Symbol notes
------------
``θ̞``
    Spanish dental fricative/approximant (Castilian "z", "ce", "ci"). Written
    with the lowering diacritic because it is normally an approximant, not the
    English fricative ``θ``.
``l̩`` / ``ɾ̩``
    Syllabic consonants: the nucleus of a syllable with no vowel, as in
    Spanish "golpe" and "puerto". They are allophones of ``l`` and ``ɾ`` but
    are listed separately because a reference that never produces them leaves
    the clone guessing on exactly the clusters that carry them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CoverageWord:
    """One catalog word and the phonemes it is responsible for.

    Attributes:
        word: The word as it must appear in the reference text.
        phonemes: Every phoneme of ``word`` in the target inventory.
    """

    word: str
    phonemes: tuple[str, ...]


#: General American English: 24 consonants + 17 vowels = 41 phonemes.
#:
#: GA merges /e/ and /ɛ/ (both spelled ``ɛ`` here) and treats the r-colored
#: schwa as ``ɚ``, so there is no separate /e/ or /ɝ/ entry.
ENGLISH_PHONEMES: tuple[str, ...] = (
    "p",
    "b",
    "t",
    "d",
    "k",
    "ɡ",
    "tʃ",
    "dʒ",
    "f",
    "v",
    "θ",
    "ð",
    "s",
    "z",
    "ʃ",
    "ʒ",
    "h",
    "m",
    "n",
    "ŋ",
    "l",
    "ɹ",
    "w",
    "j",
    "iː",
    "ɪ",
    "ɛ",
    "æ",
    "ɑ",
    "ɔ",
    "ʊ",
    "uː",
    "ʌ",
    "ɝ",
    "ə",
    "ɚ",
    "eɪ",
    "aɪ",
    "ɔɪ",
    "aʊ",
    "oʊ",
)

#: Castilian Spanish: 20 consonants + 5 vowels + 5 diphthongs + 2 syllabic
#: consonants = 32 phonemes. ``ʎ`` is excluded on purpose: the catalog asks for
#: "no regional accent", which in practice means yeísta /ʝ/.
SPANISH_PHONEMES: tuple[str, ...] = (
    "p",
    "b",
    "t",
    "d",
    "k",
    "ɡ",
    "f",
    "s",
    "x",
    "θ̞",
    "ʝ",
    "tʃ",
    "m",
    "n",
    "ɲ",
    "l",
    "ɾ",
    "r",
    "β",
    "ɣ",
    "a",
    "e",
    "i",
    "o",
    "u",
    "ai",
    "ei",
    "oi",
    "au",
    "eu",
    "l̩",
    "ɾ̩",
)

#: English words drawn from the reference text, each tagged with its phonemes.
ENGLISH_COVERAGE_WORDS: tuple[CoverageWord, ...] = (
    CoverageWord("welcome", ("w", "ɛ", "l", "k", "ə", "m")),
    CoverageWord("morning", ("m", "ɔ", "ɹ", "n", "ɪ", "ŋ")),
    CoverageWord("briefing", ("b", "ɹ", "iː", "f", "ɪ", "ŋ")),
    CoverageWord("three", ("θ", "ɹ", "iː")),
    CoverageWord("things", ("θ", "ɪ", "ŋ", "z")),
    CoverageWord("worth", ("w", "ɝ", "θ")),
    CoverageWord("knowing", ("n", "oʊ", "ɪ", "ŋ")),
    CoverageWord("before", ("b", "ɪ", "f", "ɔ", "ɹ")),
    CoverageWord("markets", ("m", "ɑ", "ɹ", "k", "ɪ", "t", "s")),
    CoverageWord("open", ("oʊ", "p", "ə", "n")),
    CoverageWord("vote", ("v", "oʊ", "t")),
    CoverageWord("went", ("w", "ɛ", "n", "t")),
    CoverageWord("through", ("θ", "ɹ", "uː")),
    CoverageWord("overnight", ("oʊ", "v", "ɚ", "n", "aɪ", "t")),
    CoverageWord("factory", ("f", "æ", "k", "t", "ə", "ɹ", "i")),
    CoverageWord("Ohio", ("oʊ", "h", "aɪ", "oʊ")),
    CoverageWord("shipping", ("ʃ", "ɪ", "p", "ɪ", "ŋ")),
    CoverageWord("chips", ("tʃ", "ɪ", "p", "s")),
    CoverageWord("six-figure", ("s", "ɪ", "k", "s", "f", "ɪ", "ɡ", "j", "ɚ")),
    CoverageWord("glitch", ("ɡ", "l", "ɪ", "tʃ")),
    CoverageWord("just", ("dʒ", "ʌ", "s", "t")),
    CoverageWord("left", ("l", "ɛ", "f", "t")),
    CoverageWord("thousand", ("θ", "aʊ", "z", "ə", "n", "d")),
    CoverageWord("customers", ("k", "ʌ", "s", "t", "ə", "m", "ɚ", "z")),
    CoverageWord("without", ("w", "ɪ", "ð", "aʊ", "t")),
    CoverageWord("power", ("p", "aʊ", "ɚ")),
    CoverageWord("Good", ("ɡ", "ʊ", "d")),
    CoverageWord("luck", ("l", "ʊ", "k")),
    CoverageWord("people", ("p", "iː", "p", "ə", "l")),
    CoverageWord("working", ("w", "ɝ", "k", "ɪ", "ŋ")),
    CoverageWord("from", ("f", "ɹ", "ʌ", "m")),
    CoverageWord("home", ("h", "oʊ", "m")),
    CoverageWord("today", ("t", "ə", "d", "eɪ")),
    CoverageWord("usually", ("j", "uː", "ʒ", "ə", "l", "i")),
    CoverageWord("think", ("θ", "ŋ", "ɪ", "k")),
    CoverageWord("this", ("ð", "ɪ", "s")),
    CoverageWord("right", ("ɹ", "aɪ", "t")),
    CoverageWord("choice", ("tʃ", "ɔɪ", "s")),
    CoverageWord("would", ("w", "ʊ", "d")),
    CoverageWord("very", ("v", "ɛ", "ɹ", "i")),
    CoverageWord("much", ("m", "ʌ", "tʃ")),
    CoverageWord("like", ("l", "aɪ", "k")),
    CoverageWord("hear", ("h", "ɪ", "ɹ")),
    CoverageWord("your", ("j", "ɔ", "ɹ")),
    CoverageWord("thoughts", ("θ", "ɔ", "t", "s")),
)

#: Spanish words drawn from the reference text, each tagged with its phonemes.
SPANISH_COVERAGE_WORDS: tuple[CoverageWord, ...] = (
    CoverageWord("Buenos", ("b", "u", "e", "n", "o", "s")),
    CoverageWord("días", ("d", "i", "a", "s")),
    CoverageWord("Bienvenidos", ("b", "i", "e", "n", "b", "e", "n", "i", "d", "o", "s")),
    CoverageWord("resumen", ("r", "e", "s", "u", "m", "e", "n")),
    CoverageWord("mañana", ("m", "a", "ɲ", "a", "n", "a")),
    CoverageWord("Hoy", ("oi",)),
    CoverageWord("repasamos", ("r", "e", "p", "a", "s", "a", "m", "o", "s")),
    CoverageWord("las", ("l", "a", "s")),
    CoverageWord("noticias", ("n", "o", "t", "i", "θ̞", "i", "a", "s")),
    CoverageWord("tecnología", ("t", "e", "k", "n", "o", "l", "o", "x", "i", "a")),
    CoverageWord("mercados", ("m", "e", "ɾ", "k", "a", "d", "o", "s")),
    CoverageWord("cultura", ("k", "u", "l", "t", "u", "ɾ", "a")),
    CoverageWord(
        "corresponsales",
        ("k", "o", "r", "r", "e", "s", "p", "o", "n", "s", "a", "l", "e", "s"),
    ),
    CoverageWord("Cada", ("k", "a", "ð̞", "a")),
    CoverageWord("titular", ("t", "i", "t", "u", "l", "a", "ɾ")),
    CoverageWord("llega", ("ʝ", "e", "ɣ", "a")),
    CoverageWord("mucho", ("m", "u", "tʃ", "o")),
    CoverageWord("contexto", ("k", "o", "n", "t", "e", "k", "s", "t", "o")),
    CoverageWord("análisis", ("a", "n", "a", "l", "i", "s", "i", "s")),
    CoverageWord("nombre", ("n", "o", "m", "b", "ɾ̩", "e")),
    CoverageWord("puerto", ("p", "w", "e", "ɾ̩", "t", "o")),
    CoverageWord("golpe", ("ɡ", "o", "l̩", "p", "e")),
    CoverageWord("banco", ("b", "a", "n", "k", "o")),
    CoverageWord("central", ("s", "e", "n", "t", "ɾ̩", "a", "l")),
    CoverageWord("cifras", ("θ̞", "i", "f", "ɾ", "a", "s")),
    CoverageWord("agua", ("a", "ɣ", "u", "a")),
    CoverageWord("nube", ("n", "u", "β", "e")),
    CoverageWord("confirman", ("k", "o", "n", "f", "i", "ɾ̩", "m", "a", "n")),
    CoverageWord("Aunque", ("au", "n", "k", "e")),
    CoverageWord("museo", ("m", "u", "s", "e", "o")),
    CoverageWord("cierra", ("θ̞", "i", "e", "r", "a")),
    CoverageWord("veinte", ("b", "ei", "n", "t", "e")),
    CoverageWord("hoy", ("oi",)),
    CoverageWord("hay", ("ai",)),
    CoverageWord("coro", ("k", "o", "ɾ", "o")),
    CoverageWord("euro", ("eu", "ɾ", "o")),
    CoverageWord("aceite", ("a", "θ̞", "e", "i", "t", "e")),
    CoverageWord("Jerez", ("x", "e", "ɾ", "e", "θ̞")),
    CoverageWord("plaza", ("p", "l", "a", "θ̞", "a")),
)

_INVENTORIES: dict[str, tuple[str, ...]] = {
    "en": ENGLISH_PHONEMES,
    "es": SPANISH_PHONEMES,
}

_COVERAGE_WORDS: dict[str, tuple[CoverageWord, ...]] = {
    "en": ENGLISH_COVERAGE_WORDS,
    "es": SPANISH_COVERAGE_WORDS,
}


def inventory(language: str) -> tuple[str, ...]:
    """Return the phoneme inventory a reference text must cover.

    Args:
        language: ISO 639-1 code.

    Returns:
        The phoneme tuple.

    Raises:
        ValueError: If the language has no declared inventory.
    """
    try:
        return _INVENTORIES[language]
    except KeyError:
        raise ValueError(f"no phoneme inventory for language: {language!r}") from None


def coverage_words(language: str) -> tuple[CoverageWord, ...]:
    """Return the curated words that together cover the inventory.

    Args:
        language: ISO 639-1 code.

    Returns:
        The coverage words.

    Raises:
        ValueError: If the language has no declared coverage words.
    """
    try:
        return _COVERAGE_WORDS[language]
    except KeyError:
        raise ValueError(f"no coverage words for language: {language!r}") from None


def covered_phonemes(language: str) -> set[str]:
    """Return the phonemes the coverage words actually hit.

    Args:
        language: ISO 639-1 code.

    Returns:
        Set of phoneme symbols.
    """
    covered: set[str] = set()
    for word in coverage_words(language):
        covered.update(word.phonemes)
    return covered


def missing_phonemes(language: str) -> set[str]:
    """Return the inventory phonemes no coverage word hits.

    Args:
        language: ISO 639-1 code.

    Returns:
        Set of phoneme symbols that are not covered.
    """
    return set(inventory(language)) - covered_phonemes(language)


def words_missing_from(language: str, ref_text: str) -> list[str]:
    """Return the coverage words a reference text does not contain.

    Args:
        language: ISO 639-1 code.
        ref_text: Reference transcript to check.

    Returns:
        Coverage words absent from ``ref_text``, in catalog order.
    """
    text = ref_text.casefold()
    return [
        word.word
        for word in coverage_words(language)
        if not re.search(rf"\b{re.escape(word.word.casefold())}\b", text)
    ]
