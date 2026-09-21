"""Unit tests for the collections.toml loader and helpers."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.core.collections import (
    CollectionsLoader,
    default_collection,
    get_collection,
    load_collections,
)

SAMPLE_TOML = """\
[collection_defaults]
language = "es"
voice = "es-mx-male"

[[collections]]
name = "tech"
time = "08:00"
feed_urls = ["https://techcrunch.com/feed/", "https://www.theverge.com/rss/index.xml"]
prompt_template = "default"

[[collections]]
name = "health"
time = "09:30"
feed_urls = ["https://www.healthline.com/rss"]
language = "en"
voice = "en-gb-female"

[[collections]]
name = "paused"
time = "17:15"
feed_urls = ["https://feeds.bloomberg.com/markets/news.rss"]
enabled = false
"""


def _write_collections(tmp_path: Path, content: str) -> Path:
    """Write collections content to a temp file and return its path."""
    path = tmp_path / "collections.toml"
    path.write_text(content, encoding="utf-8")
    return path


def test_loader_parses_collections(tmp_path: Path) -> None:
    """Loads all entries and applies collection_defaults where not overridden."""
    path = _write_collections(tmp_path, SAMPLE_TOML)
    loader = CollectionsLoader(str(path))
    collections = loader.load()

    assert len(collections) == 3

    tech = next(c for c in collections if c.name == "tech")
    assert tech.time == "08:00"
    assert tech.language == "es"  # inherited from defaults
    assert tech.voice == "es-mx-male"
    assert tech.feed_urls == [
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
    ]
    assert tech.enabled is True

    health = next(c for c in collections if c.name == "health")
    assert health.language == "en"  # overridden
    assert health.voice == "en-gb-female"

    paused = next(c for c in collections if c.name == "paused")
    assert paused.enabled is False


def test_loader_enabled_filters_disabled(tmp_path: Path) -> None:
    """enabled() returns only enabled collections."""
    path = _write_collections(tmp_path, SAMPLE_TOML)
    loader = CollectionsLoader(str(path))
    enabled = loader.enabled()

    names = [c.name for c in enabled]
    assert "paused" not in names
    assert "tech" in names
    assert "health" in names


def test_loader_missing_file_returns_empty(tmp_path: Path) -> None:
    """A missing collections file yields an empty list instead of an error."""
    loader = CollectionsLoader(str(tmp_path / "nope.toml"))
    assert loader.load() == []


def test_loader_invalid_time(tmp_path: Path) -> None:
    """A malformed time field raises a validation error."""
    path = _write_collections(
        tmp_path,
        '[[collections]]\nname = "bad"\ntime = "25:61"\nfeed_urls = ["https://x.com/rss"]\n',
    )
    loader = CollectionsLoader(str(path))
    with pytest.raises(ValidationError):
        loader.load()


def test_loader_normalizes_time(tmp_path: Path) -> None:
    """A single-digit time is normalized to zero-padded HH:MM."""
    path = _write_collections(
        tmp_path,
        '[[collections]]\nname = "tech"\ntime = "8:5"\nfeed_urls = ["https://x.com/rss"]\n',
    )
    collection = CollectionsLoader(str(path)).load()[0]
    assert collection.time == "08:05"


def test_loader_assigns_indexed_name_when_missing(tmp_path: Path) -> None:
    """An entry without a name falls back to an indexed name."""
    path = _write_collections(tmp_path, '[[collections]]\nfeed_urls = ["https://x.com/rss"]\n')
    collection = CollectionsLoader(str(path)).load()[0]
    assert collection.name == "collection-1"


def test_default_delivery_channels_is_telegram(tmp_path: Path) -> None:
    """Collections without delivery_channels default to Telegram delivery."""
    path = _write_collections(
        tmp_path, '[[collections]]\nname = "tech"\nfeed_urls = ["https://x.com/rss"]\n'
    )
    collection = CollectionsLoader(str(path)).load()[0]
    assert collection.delivery_channels == ["telegram"]


def test_delivery_channels_parsed_and_normalized(tmp_path: Path) -> None:
    """Known channels are kept in order; unknown values and duplicates dropped."""
    path = _write_collections(
        tmp_path,
        '[[collections]]\nname = "tech"\nfeed_urls = ["https://x.com/rss"]\n'
        'delivery_channels = ["email", "telegram", "telegram", "sms"]\n',
    )
    collection = CollectionsLoader(str(path)).load()[0]
    assert collection.delivery_channels == ["telegram", "email"]


def test_empty_delivery_channels_falls_back_to_telegram(tmp_path: Path) -> None:
    """An empty delivery_channels list defaults to telegram delivery."""
    path = _write_collections(
        tmp_path,
        '[[collections]]\nname = "tech"\nfeed_urls = ["https://x.com/rss"]\n'
        "delivery_channels = []\n",
    )
    collection = CollectionsLoader(str(path)).load()[0]
    assert collection.delivery_channels == ["telegram"]


def test_delivery_channels_accept_whatsapp(tmp_path: Path) -> None:
    """The whatsapp channel is a recognized delivery channel."""
    path = _write_collections(
        tmp_path,
        '[[collections]]\nname = "tech"\nfeed_urls = ["https://x.com/rss"]\n'
        'delivery_channels = ["whatsapp", "unknown"]\n',
    )
    collection = CollectionsLoader(str(path)).load()[0]
    assert collection.delivery_channels == ["whatsapp"]


def test_logic_delivery_defaults_to_none(tmp_path: Path) -> None:
    """Collections without logic_delivery do not generate any logic text."""
    path = _write_collections(
        tmp_path, '[[collections]]\nname = "tech"\nfeed_urls = ["https://x.com/rss"]\n'
    )
    collection = CollectionsLoader(str(path)).load()[0]
    assert collection.logic_delivery is None


def test_logic_delivery_parsed_from_inline_table(tmp_path: Path) -> None:
    """An inline logic_delivery table is parsed into a LogicDeliveryConfig."""
    path = _write_collections(
        tmp_path,
        '[[collections]]\nname = "health"\nfeed_urls = ["https://x.com/rss"]\n'
        'logic_delivery = { channels = ["email"], '
        'email_recipients = ["admin@example.com"] }\n',
    )
    collection = CollectionsLoader(str(path)).load()[0]
    assert collection.logic_delivery is not None
    assert collection.logic_delivery.channels == ["email"]
    assert collection.logic_delivery.email_recipients == ["admin@example.com"]
    assert collection.logic_delivery.whatsapp_recipients == []


def test_logic_delivery_channels_fall_back_to_telegram(tmp_path: Path) -> None:
    """Empty or unknown logic_delivery channels default to telegram."""
    path = _write_collections(
        tmp_path,
        '[[collections]]\nname = "tech"\nfeed_urls = ["https://x.com/rss"]\n'
        'logic_delivery = { channels = ["sms", "unknown"] }\n',
    )
    collection = CollectionsLoader(str(path)).load()[0]
    assert collection.logic_delivery is not None
    assert collection.logic_delivery.channels == ["telegram"]


def test_logic_delivery_rejects_invalid_whatsapp(tmp_path: Path) -> None:
    """Non-E.164 logic_delivery whatsapp recipients are rejected at parse time."""
    path = _write_collections(
        tmp_path,
        '[[collections]]\nname = "tech"\nfeed_urls = ["https://x.com/rss"]\n'
        'logic_delivery = { channels = ["whatsapp"], '
        'whatsapp_recipients = ["123"] }\n',
    )
    with pytest.raises(ValidationError):
        CollectionsLoader(str(path)).load()


def test_whatsapp_recipients_must_be_e164(tmp_path: Path) -> None:
    """Non-E.164 whatsapp_recipients values fail validation."""
    path = _write_collections(
        tmp_path,
        '[[collections]]\nname = "tech"\nfeed_urls = ["https://x.com/rss"]\n'
        'whatsapp_recipients = ["34612345678", "+3X"]\n',
    )
    loader = CollectionsLoader(str(path))
    with pytest.raises(ValidationError):
        loader.load()


def test_whatsapp_fields_parsed(tmp_path: Path) -> None:
    """Valid E.164 recipients and a group id are parsed."""
    path = _write_collections(
        tmp_path,
        '[[collections]]\nname = "tech"\nfeed_urls = ["https://x.com/rss"]\n'
        'whatsapp_recipients = ["+34612345678"]\nwhatsapp_group_id = "1234567890"\n',
    )
    collection = CollectionsLoader(str(path)).load()[0]
    assert collection.whatsapp_recipients == ["+34612345678"]
    assert collection.whatsapp_group_id == "1234567890"


def test_get_collection_lookup(tmp_path: Path) -> None:
    """get_collection finds a collection by name."""
    path = _write_collections(tmp_path, SAMPLE_TOML)
    tech = get_collection("tech", path=str(path))
    assert tech is not None
    assert tech.name == "tech"
    assert get_collection("missing", path=str(path)) is None


def test_load_collections_cache_respects_path(tmp_path: Path) -> None:
    """load_collections is cached per path but not keyed on the default path."""
    path = _write_collections(tmp_path, SAMPLE_TOML)
    first = load_collections(str(path))
    second = load_collections(str(path))
    assert first == second
    assert len(first) == 3


def test_default_collection_uses_settings(tmp_path: Path) -> None:
    """default_collection mirrors the legacy single-digest config."""
    collection = default_collection()
    assert collection.name == "default"
    assert collection.prompt_template == "default"
    assert isinstance(collection.feed_urls, list)
