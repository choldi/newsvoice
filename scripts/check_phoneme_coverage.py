"""Verify that voice reference texts cover the full phoneme inventory.

Two independent checks, because either one alone is meaningless:

1. **Inventory coverage** — the curated coverage words of a language must hit
   every phoneme in its inventory. This fails when a phoneme is declared but no
   word exercises it.
2. **Catalog coverage** — every ``ref_text`` in the catalog must actually contain
   those words. This fails when someone edits a reference transcript and
   silently drops the only word that carried a rare phoneme.

Run it before committing a catalog change:

    python scripts/check_phoneme_coverage.py
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from src.core.phoneme_inventory import (
    coverage_words,
    inventory,
    missing_phonemes,
    words_missing_from,
)
from src.core.voice_catalog import load_profiles


def _check_language(language: str, ref_texts: set[str]) -> list[str]:
    """Return every problem found for one language.

    Args:
        language: ISO 639-1 code.
        ref_texts: Distinct reference transcripts declared for the language.

    Returns:
        Human-readable problem lines; empty when the language is clean.
    """
    problems: list[str] = []

    absent = sorted(missing_phonemes(language))
    if absent:
        problems.append(f"  inventory not covered, missing: {', '.join(absent)}")

    if len(ref_texts) > 1:
        problems.append(
            f"  {len(ref_texts)} distinct ref_texts for one language; "
            "they must be identical so ECAPA comparisons stay comparable"
        )

    for ref_text in sorted(ref_texts):
        missing_words = words_missing_from(language, ref_text)
        if missing_words:
            problems.append(
                f"  ref_text is missing {len(missing_words)} coverage word(s): "
                f"{', '.join(missing_words)}"
            )

    return problems


def main() -> int:
    """Run the coverage checks and report the result.

    Returns:
        0 when every language is fully covered, 1 otherwise.
    """
    parser = argparse.ArgumentParser(description="Voice phoneme coverage check")
    parser.add_argument(
        "--profiles",
        default="voice_profiles.json",
        help="Catalog JSON path (default: voice_profiles.json)",
    )
    args = parser.parse_args()

    profiles = load_profiles(args.profiles)
    by_language: dict[str, set[str]] = defaultdict(set)
    for profile in profiles:
        by_language[profile.language].add(profile.ref_text)

    failures = 0
    for language in sorted(by_language):
        label = f"[{language}] {len(by_language[language])} ref_text(s)"
        problems = _check_language(language, by_language[language])
        if problems:
            failures += 1
            print(f"FAIL {label}")
            for problem in problems:
                print(problem)
        else:
            covered = len(inventory(language))
            words = len(coverage_words(language))
            print(f"OK   {label}: {covered}/{covered} phonemes via {words} words")

    if failures:
        print(f"\n{failures} language(s) failed the phoneme coverage check")
        return 1
    print("\nEvery language covers its full phoneme inventory")
    return 0


if __name__ == "__main__":
    sys.exit(main())
