"""Unit tests for the Phase 7.16 phonetic-adaptation heuristics.

Covers the migration seeds (including the drift guard against the Phase 7.15
constants), the model's textarea parsing, the repository round trip and the
service contracts the GUI and the lesson submit path rely on.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.api.models.phonetic import PhoneticHeuristicsIn
from src.api.services import phonetic_heuristics
from src.api.services.phonetic_heuristics import HeuristicsError
from src.core.respelling import builtin_heuristics


def _payload(**overrides: object) -> PhoneticHeuristicsIn:
    """Build a valid de -> es payload for one test.

    Args:
        **overrides: Field values replacing the defaults.

    Returns:
        A validated :class:`PhoneticHeuristicsIn`.
    """
    fields: dict = {
        "language_origin": "de",
        "language_target": "es",
        "language_origin_name": "German",
        "language_target_name": "Spanish",
        "suffixes": [],
        "digraphs": [],
        "tech_terms": [],
        "native_words": [],
        "examples": {},
        "ascii_only": True,
        "flag_all_foreign": False,
        "enabled": True,
    }
    fields.update(overrides)
    return PhoneticHeuristicsIn.model_validate(fields)


# ---------------------------------------------------------------------------
# Migration seeds
# ---------------------------------------------------------------------------


async def test_migration_seeds_the_supported_pairs() -> None:
    """The migration ships the narration pairs plus the lesson's French pair."""
    rows = await phonetic_heuristics.list_all()
    pairs = {(row.language_origin, row.language_target) for row in rows}

    assert {("en", "es"), ("en", "fr"), ("fr", "es")} <= pairs


async def test_list_all_orders_rows_by_pair() -> None:
    """Rows come out sorted so the admin table does not jump around."""
    rows = await phonetic_heuristics.list_all()

    keys = [(row.language_origin, row.language_target) for row in rows]
    assert keys == sorted(keys)


async def test_english_seed_matches_the_builtin_defaults() -> None:
    """Drift guard: the seeded en->es row is the Phase 7.15 constant set."""
    row = await phonetic_heuristics.get_pair("en", "es")
    builtin = builtin_heuristics("en", "es")

    assert row is not None and builtin is not None
    assert tuple(row.suffixes) == builtin.suffixes
    assert tuple(row.digraphs) == builtin.digraphs
    assert frozenset(row.tech_terms) == builtin.tech_terms
    assert frozenset(row.native_words) == builtin.native_words
    assert tuple(row.examples.items()) == builtin.examples
    assert row.ascii_only is True
    assert row.flag_all_foreign is False


async def test_french_seed_offers_every_non_native_word() -> None:
    """The lesson pair hunts short French words the shape heuristic misses."""
    row = await phonetic_heuristics.get_pair("fr", "es")

    assert row is not None
    assert row.flag_all_foreign is True, "bonjour/merci have no distinctive ending"
    assert row.ascii_only is False, "French carries accents a Spanish voice must keep"
    assert row.language_origin_name == "French"
    assert row.language_target_name == "Spanish"
    assert row.examples["bonjour"] == "bonjur"
    assert "empresa" in row.native_words


# ---------------------------------------------------------------------------
# Model parsing and validation
# ---------------------------------------------------------------------------


def test_model_parses_textarea_lists_and_examples() -> None:
    """Word lists and the example map arrive as plain textarea text."""
    model = _payload(suffixes="ch\nung, pflicht", examples="haus = os\nauto = óto")  # type: ignore[arg-type]

    assert model.suffixes == ["ch", "ung", "pflicht"]
    assert model.examples == {"haus": "os", "auto": "óto"}


def test_model_parses_json_lists() -> None:
    """A pasted JSON array is accepted too (copy from the API)."""
    model = _payload(suffixes='["ch", "ung"]', examples='{"haus": "os"}')  # type: ignore[arg-type]

    assert model.suffixes == ["ch", "ung"]
    assert model.examples == {"haus": "os"}


def test_model_rejects_a_same_language_pair() -> None:
    """A language does not need adapting to itself."""
    with pytest.raises(ValidationError, match="must differ"):
        _payload(language_origin="es", language_target="es")


@pytest.mark.parametrize("code", ["", "ESP", "e", "es-ES", "ca tal"])
def test_model_rejects_non_iso639_1_codes(code: str) -> None:
    """Only two-letter lowercase codes reach the database."""
    with pytest.raises(ValidationError, match="ISO 639-1"):
        _payload(language_target=code)


def test_model_drops_blank_and_duplicated_words() -> None:
    """Blank lines and repeats never inflate the stored list."""
    model = _payload(native_words="el\n\nla\nel\n  la  ")  # type: ignore[arg-type]

    assert model.native_words == ["el", "la"]


# ---------------------------------------------------------------------------
# Repository/service round trip
# ---------------------------------------------------------------------------


async def test_save_creates_then_updates_the_same_pair() -> None:
    """Creating without an id and replacing with one share one pair only."""
    created = await phonetic_heuristics.save(_payload(language_origin="de"))
    assert created.language_origin == "de"

    updated = await phonetic_heuristics.save(
        _payload(language_origin="de", native_words=["haus"]),  # type: ignore[arg-type]
        row_id=created.id,
    )

    assert updated.id == created.id
    assert updated.native_words == ["haus"]
    rows = [
        row
        for row in await phonetic_heuristics.list_all()
        if row.language_origin == "de" and row.language_target == created.language_target
    ]
    assert len(rows) == 1


async def test_save_rejects_a_duplicate_pair() -> None:
    """The UNIQUE(pair) index reports a readable error, not a 500."""
    with pytest.raises(HeuristicsError, match="already exists"):
        await phonetic_heuristics.save(_payload(language_origin="en", language_target="es"))


async def test_get_pair_returns_none_for_an_unconfigured_pair() -> None:
    """An unknown pair is not an error, just absent."""
    assert await phonetic_heuristics.get_pair("it", "pt") is None


async def test_delete_removes_a_row_and_reports_a_missing_one() -> None:
    """Delete is idempotent through the service's error contract."""
    created = await phonetic_heuristics.save(_payload(language_origin="pt"))

    await phonetic_heuristics.delete(created.id)

    assert await phonetic_heuristics.get(created.id) is None
    with pytest.raises(HeuristicsError, match="not found"):
        await phonetic_heuristics.delete(created.id)


async def test_get_reports_an_unknown_id_as_none() -> None:
    """The edit route uses None to redirect instead of crashing."""
    assert await phonetic_heuristics.get(999_999) is None


async def test_require_enabled_pair_walks_the_three_states() -> None:
    """Missing -> configured hint, disabled -> disabled hint, enabled -> row."""
    with pytest.raises(HeuristicsError, match="no phonetic heuristics for de -> es"):
        await phonetic_heuristics.require_enabled_pair("de", "es")

    await phonetic_heuristics.save(_payload(language_origin="de", enabled=False))
    with pytest.raises(HeuristicsError, match="disabled"):
        await phonetic_heuristics.require_enabled_pair("de", "es")

    enabled = await phonetic_heuristics.require_enabled_pair("en", "es")
    assert enabled.language_origin == "en"
