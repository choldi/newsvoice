"""Unit tests for the collections repository (TOML read/write)."""

import os
from pathlib import Path

import pytest

from src.api.services.collections_repo import (
    CollectionConflictError,
    CollectionsRepository,
)
from src.core.collections import CollectionConfig, load_collections

VALID_DEFAULTS = {
    "tts_enabled": False,
    "deliver_audio": False,
    "language": "es",
    "voice": "es-mx-male",
}

VALID_HEADER = """# collections.toml
# Test header comment that must survive round-trips.
"""


def _file(tmp_path: Path) -> Path:
    return tmp_path / "collections.toml"


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _tech(name: str = "tech") -> CollectionConfig:
    return CollectionConfig(
        name=name,
        time="08:00",
        feed_urls=["https://techcrunch.com/feed/", "https://www.theverge.com/rss/index.xml"],
        podcast_name="Tech Daily",
        prompt_template="default",
    )


def test_save_all_round_trip(tmp_path: Path) -> None:
    """Written file reloads into the exact same configs."""
    path = _file(tmp_path)
    _write(path, VALID_HEADER + "[collection_defaults]\ntts_enabled = false\n")
    repo = CollectionsRepository(str(path))

    collections = [_tech(), CollectionConfig(name="health", time="09:30")]
    repo.save_all(collections)

    reloaded: dict[str, CollectionConfig] = {
        c.name: c for c in CollectionsRepository(str(path)).list()
    }
    assert set(reloaded) == {"tech", "health"}
    assert reloaded["tech"].podcast_name == "Tech Daily"
    assert reloaded["tech"].time == "08:00"

    load_collections.cache_clear()


def test_save_all_preserves_collection_defaults(tmp_path: Path) -> None:
    """The [collection_defaults] table survives a rewrite."""
    path = _file(tmp_path)
    _write(
        path,
        VALID_HEADER
        + "[collection_defaults]\n"
        + "tts_enabled = false\n"
        + "language = 'es'\n"
        + "voice = 'es-mx-male'\n"
        + "[[collections]]\nname = 'tech'\n",
    )
    CollectionsRepository(str(path)).save_all([_tech()])

    defaults = CollectionsRepository(str(path))._read_defaults_table()
    assert defaults["language"] == "es"
    assert defaults["voice"] == "es-mx-male"
    load_collections.cache_clear()


def test_create_and_conflict(tmp_path: Path) -> None:
    """Create adds a collection; duplicate names are rejected."""
    path = _file(tmp_path)
    _write(path, VALID_HEADER + "[collection_defaults]\n")
    repo = CollectionsRepository(str(path))

    repo.create(_tech())
    assert repo.exists("tech")

    with pytest.raises(CollectionConflictError):
        repo.create(_tech())
    load_collections.cache_clear()


def test_update_and_delete(tmp_path: Path) -> None:
    """Update overwrites an existing collection; delete removes it."""
    path = _file(tmp_path)
    _write(path, VALID_HEADER + "[collection_defaults]\n")
    repo = CollectionsRepository(str(path))

    created = repo.create(_tech())
    repo.create(CollectionConfig(name="health", time="09:30"))

    updated = created.model_copy(update={"time": "12:15", "tts_enabled": True})
    repo.create(updated, overwrite=True)

    names = {c.name: c for c in repo.list()}
    assert names["tech"].time == "12:15"
    assert names["tech"].tts_enabled is True

    repo.save_all([names["health"]])
    assert [c.name for c in repo.list()] == ["health"]
    load_collections.cache_clear()


def test_disable_flag_preserved(tmp_path: Path) -> None:
    """A disabled collection keeps enabled=false in the file."""
    path = _file(tmp_path)
    _write(path, VALID_HEADER + "[collection_defaults]\n")
    repo = CollectionsRepository(str(path))

    tech = _tech()
    disabled = tech.model_copy(update={"enabled": False})
    repo.save_all([disabled])

    raw = path.read_text(encoding="utf-8")
    assert "enabled = false" in raw


def test_duplicate_names_rejected_on_save(tmp_path: Path) -> None:
    """save_all rejects duplicate names before touching the file."""
    path = _file(tmp_path)
    _write(path, VALID_HEADER + "[collection_defaults]\n")
    repo = CollectionsRepository(str(path))

    with pytest.raises(ValueError):
        repo.save_all([_tech(), _tech()])
    load_collections.cache_clear()


def test_write_is_atomic(tmp_path: Path) -> None:
    """save_all does not leave a partial file behind."""
    path = _file(tmp_path)
    _write(path, VALID_HEADER + "[collection_defaults]\n")
    repo = CollectionsRepository(str(path))

    original = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        repo.save_all([_tech(), _tech()])  # duplicate names -> abort

    assert path.read_text(encoding="utf-8") == original
    load_collections.cache_clear()


def test_save_all_falls_back_when_replace_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EBUSY on os.replace (bind-mounted file) falls back to in-place write."""
    path = _file(tmp_path)
    _write(path, VALID_HEADER + "[collection_defaults]\n")
    repo = CollectionsRepository(str(path))

    original_replace = os.replace

    def busy_replace(src: str, dst: str) -> None:
        """Simulate a bind-mounted target the same way docker does."""
        if dst == str(path):
            raise OSError(16, "Device or resource busy", dst)
        original_replace(src, dst)

    monkeypatch.setattr("src.api.services.collections_repo.os.replace", busy_replace)
    repo.save_all([_tech()])

    reloaded = CollectionsRepository(str(path)).list()
    assert [c.name for c in reloaded] == ["tech"]
    load_collections.cache_clear()


def test_delivery_channels_round_trip(tmp_path: Path) -> None:
    """delivery_channels persists through the TOML round trip."""
    path = _file(tmp_path)
    _write(path, VALID_HEADER + "[collection_defaults]\n")
    repo = CollectionsRepository(str(path))
    tech = _tech().model_copy(update={"delivery_channels": ["telegram", "email"]})
    repo.save_all([tech])

    raw = path.read_text(encoding="utf-8")
    assert "delivery_channels = ['telegram', 'email']" in raw

    reloaded = CollectionsRepository(str(path)).list()[0]
    assert reloaded.delivery_channels == ["telegram", "email"]
    load_collections.cache_clear()


def test_logic_delivery_round_trip(tmp_path: Path) -> None:
    """logic_delivery persists through the TOML round trip as an inline table."""
    from src.core.collections import LogicDeliveryConfig

    path = _file(tmp_path)
    _write(path, VALID_HEADER + "[collection_defaults]\n")
    repo = CollectionsRepository(str(path))
    tech = _tech().model_copy(
        update={
            "logic_delivery": LogicDeliveryConfig(
                channels=["email"], email_recipients=["admin@example.com"]
            )
        }
    )
    repo.save_all([tech])

    raw = path.read_text(encoding="utf-8")
    expected_pairs = (
        "logic_delivery = { channels = ['email'], email_recipients = ['admin@example.com'] }"
    )
    assert expected_pairs in raw

    reloaded = CollectionsRepository(str(path)).list()[0]
    assert reloaded.logic_delivery is not None
    assert reloaded.logic_delivery.channels == ["email"]
    assert reloaded.logic_delivery.email_recipients == ["admin@example.com"]
    load_collections.cache_clear()


def test_save_all_multiline_custom_prompt(tmp_path: Path) -> None:
    """A multi-line custom_prompt survives the TOML round trip as valid TOML."""
    path = _file(tmp_path)
    _write(path, VALID_HEADER + "[collection_defaults]\n")
    prompt = 'Opening:\nStart with:\n\n"Tech news!\u201d\n\n{{RSS_ARTICLES}}'
    tech = _tech().model_copy(update={"prompt_template": "default", "custom_prompt": prompt})
    CollectionsRepository(str(path)).save_all([tech])

    import tomllib

    with path.open("rb") as fh:
        tomllib.load(fh)  # must parse without error

    reloaded = CollectionsRepository(str(path)).list()
    assert reloaded[0].custom_prompt == prompt
    load_collections.cache_clear()
