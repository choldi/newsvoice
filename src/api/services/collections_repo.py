"""Collections repository: read/write collections.toml for the admin GUI.

The repository keeps `collections.toml` as the single source of truth for
collection configuration (Phase 5). It offers the CRUD operations the Phase 6
GUI needs: list, get, create, update, rename, delete — plus `save_all` which
atomically rewrites the file preserving the preamble and the
`[collection_defaults]` table, while normalizing the `[[collections]]` blocks.

The serializer is intentionally small and dependency-free (no tomli_w); every
written value passes through pydantic validation on the next load, so a bad
write fails fast in tests instead of corrupting production config.
"""

from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path
from typing import Any

import structlog

from src.core.collections import CollectionConfig, CollectionsLoader, load_collections
from src.core.config import get_settings

logger = structlog.get_logger(__name__)

# Fields written for every `[[collections]]` block, in canonical field order.
# It must cover every `CollectionConfig` field: `tests/unit/test_collections_repo.py`
# guards that, because a missing key silently drops configuration on any rewrite.
_COLLECTION_KEYS = (
    "name",
    "time",
    "feed_urls",
    "tts_enabled",
    "deliver_audio",
    "podcast_name",
    "podcast_behavior",
    "language",
    "voice",
    "prompt_template",
    "custom_prompt",
    "chat_id",
    "email_recipients",
    "whatsapp_recipients",
    "whatsapp_group_id",
    "delivery_channels",
    "logic_delivery",
    "enabled",
)


def _toml_string(value: str) -> str:
    """Quote a string as TOML, keeping it on a single line.

    Uses single-quoted literals when the value is safe (no newlines, tabs, or
    CR); otherwise falls back to a double-quoted basic string with escaped
    control characters. TOML literal strings cannot contain newlines, so this
    matters for multi-line fields such as ``custom_prompt``.

    Args:
        value: Raw string value.

    Returns:
        A single-line TOML string literal.
    """
    if not (
        "'" in value
        or '"' in value
        or "\\" in value
        or "\t" in value
        or "\n" in value
        or "\r" in value
    ):
        return f"'{value}'"
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'


def _toml_value(value: Any) -> str:
    """Serialize a scalar as a TOML value.

    Args:
        value: A dict (single-line for arrays) or scalar.

    Returns:
        Minimal TOML representation.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, list):
        items = ", ".join(_toml_string(item) for item in value if isinstance(item, str))
        return f"[{items}]"
    if isinstance(value, dict):
        _empty_values = ("", [], {})
        pairs = ", ".join(
            f"{k} = {_toml_value(v)}"
            for k, v in value.items()
            if v is not None and v not in _empty_values
        )
        return f"{{ {pairs} }}"
    return _toml_string(str(value))


class CollectionConflictError(ValueError):
    """Raised when a collection name collides with an existing one."""


class CollectionsRepository:
    """CRUD repository over the collections.toml file."""

    def __init__(self, path: str | None = None) -> None:
        """Initialize the repository with an optional explicit TOML path.

        Args:
            path: Path to collections.toml. Defaults to settings.collections_file.
        """
        self.path = Path(path or get_settings().collections_file)
        self._loader = CollectionsLoader(path=str(self.path))

    def _load(self) -> list[CollectionConfig]:
        """Load (and cache-bust) all collections from the file.

        Returns:
            List of CollectionConfig entries (enabled + disabled).
        """
        load_collections.cache_clear()
        return self._loader.load()

    def list(self) -> list[CollectionConfig]:
        """Return all collections (enabled and disabled).

        Returns:
            List of CollectionConfig entries.
        """
        return self._load()

    def get(self, name: str) -> CollectionConfig | None:
        """Return one collection by name (case-insensitive).

        Args:
            name: Collection name to look up.

        Returns:
            The matching CollectionConfig or None.
        """
        return next((c for c in self._load() if c.name.casefold() == name.casefold()), None)

    def exists(self, name: str) -> bool:
        """Return whether a collection with this name already exists.

        Args:
            name: Collection name to check.

        Returns:
            True if a collection matches, case-insensitively.
        """
        return self.get(name) is not None

    def create(self, config: CollectionConfig, overwrite: bool = False) -> CollectionConfig:
        """Add a new collection to the file, validating uniqueness.

        Args:
            config: CollectionConfig to persist.
            overwrite: Allow replacing an existing collection with the same name.

        Returns:
            The persisted CollectionConfig.

        Raises:
            CollectionConflictError: If the name already exists and overwrite is False.
        """
        if self.exists(config.name) and not overwrite:
            raise CollectionConflictError(f"A collection named '{config.name}' already exists.")
        collections = [c for c in self._load() if config.name.casefold() != c.name.casefold()]
        collections.append(config)
        self.save_all(collections)
        return config

    def save_all(self, collections: list[CollectionConfig]) -> None:
        """Persist the whole list of collections atomically.

        The file's preamble and `[collection_defaults]` table are preserved;
        the `[[collections]]` blocks are regenerated from the provided configs.

        Args:
            collections: Full list of CollectionConfig to write.

        Raises:
            ValueError: If two collections share a name.
        """
        seen: set[str] = set()
        for config in collections:
            key = config.name.casefold()
            if key in seen:
                raise ValueError(f"Duplicate collection name: '{config.name}'")
            seen.add(key)
            CollectionConfig(**config.model_dump())  # validate before writing

        raw = self._loader.load()
        _ = raw  # parse check; ensures the current file is valid before rewriting

        defaults = self._read_defaults_table()

        document = self._document(collections, defaults=defaults)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, prefix=".collections-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(document)
            try:
                os.replace(tmp_name, self.path)
            except OSError as exc:
                if exc.errno == errno.EBUSY:
                    # The target is a bind-mounted file (docker `./collections.toml`
                    # volume): rename over a mount point yields EBUSY. Write the
                    # document in place instead (requires a rw mount).
                    logger.warning("replace_failed_ebusy_writing_in_place", path=str(self.path))
                    with self.path.open("w", encoding="utf-8") as handle:
                        handle.write(document)
                else:
                    raise
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        load_collections.cache_clear()
        logger.info("collections_saved", path=str(self.path), count=len(collections))

    def _read_defaults_table(self) -> dict[str, Any]:
        """Extract the raw [collection_defaults] table, if present.

        Returns:
            Dict of defaults parsed from the file (empty if absent/invalid).
        """
        if not self.path.exists():
            return {}
        import tomllib

        try:
            with self.path.open("rb") as fh:
                raw = tomllib.load(fh)
        except (tomllib.TOMLDecodeError, FileNotFoundError):
            return {}
        return dict(raw.get("collection_defaults", {}) or {})

    def _document(self, collections: list[CollectionConfig], defaults: dict[str, Any]) -> str:
        """Render the full TOML document text.

        Args:
            collections: List of collections to serialize.
            defaults: Raw defaults table to preserve.

        Returns:
            TOML document as a string.
        """
        lines = [
            "# collections.toml",
            "# One entry per independent news digest. Generated by the admin GUI.",
            "# `time` is HH:MM 24-hour UTC. `enabled = false` pauses the digest.",
            "",
        ]
        if defaults:
            lines.append("[collection_defaults]")
            for key, value in defaults.items():
                lines.append(f"{key} = {_toml_value(value)}")
            lines.append("")

        for config in collections:
            lines.append("[[collections]]")
            data = config.model_dump()
            for key in _COLLECTION_KEYS:
                value = data.get(key)
                if value is None:
                    continue
                lines.append(f"{key} = {_toml_value(value)}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"
