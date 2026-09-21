"""Collection configuration loaded from collections.toml.

Each collection describes one independent news digest: its RSS feeds, delivery
preferences, prompt template, and the time (HH:MM) it runs on its own Temporal
schedule. Inspired by Better Morning's collections.toml layout.

Shared defaults can be declared once under a `[collection_defaults]` table and
individual `[[collections]]` entries override them.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import structlog
from pydantic import BaseModel, Field, field_validator

from src.core.config import get_settings

logger = structlog.get_logger()

DEFAULT_COLLECTION_NAME = "default"
DEFAULT_COLLECTION_TIME = "08:00"
DEFAULT_PROMPT_TEMPLATE = "default"


class LogicDeliveryConfig(BaseModel):
    """Optional admin destination for the LLM news-selection logic.

    When present on a collection, the pipeline asks the LLM (in a separate
    call) to explain the news-selection logic used for the digest and sends it
    to these destinations as a message apart from the digest. When absent, the
    LLM does not generate any logic text.
    """

    channels: list[str] = Field(
        default_factory=lambda: ["telegram"],
        description=(
            "Delivery channels ('telegram', 'email', 'whatsapp'); empty or unset"
            " defaults to telegram"
        ),
    )
    chat_id: str | None = Field(
        default=None,
        description="Telegram chat id override; global TELEGRAM_CHAT_ID if empty",
    )
    email_recipients: list[str] = Field(
        default_factory=list,
        description="Email recipients addressed per logic delivery; empty disables email",
    )
    whatsapp_recipients: list[str] = Field(
        default_factory=list,
        description="WhatsApp recipients (E.164) addressed per logic delivery",
    )
    whatsapp_group_id: str | None = Field(
        default=None,
        description="WhatsApp group id delivered as <id>@g.us, exempt from the allowlist",
    )

    @field_validator("channels")
    @classmethod
    def _channels(cls, value: list[str]) -> list[str]:
        """Keep only known channels, de-duplicated, falling back to telegram."""
        known = ("telegram", "email", "whatsapp")
        channels = [channel for channel in known if channel in value]
        return channels or ["telegram"]

    @field_validator("whatsapp_recipients")
    @classmethod
    def _whatsapp_recipients(cls, value: list[str]) -> list[str]:
        """Reject non-E.164 numbers; an empty list still disables the channel."""
        clean = [num.strip() for num in value if num.strip()]
        invalid = [num for num in clean if not re.fullmatch(r"\+[1-9]\d{1,14}", num)]
        if invalid:
            raise ValueError(f"whatsapp_recipients must be E.164 numbers: {invalid}")
        return clean


class CollectionConfig(BaseModel):
    """Configuration for a single news digest collection."""

    name: str = Field(..., description="Unique collection identifier (e.g. 'tech')")
    feed_urls: list[str] = Field(
        default_factory=list, description="RSS feed URLs for this collection"
    )
    time: str = Field(
        default=DEFAULT_COLLECTION_TIME,
        description="Schedule time in HH:MM (UTC) for this collection's digest",
    )
    tts_enabled: bool = Field(default=False, description="Generate audio for this digest")
    deliver_audio: bool = Field(default=False, description="Deliver the generated audio")
    podcast_name: str = Field(
        default="",
        description="Podcast name displayed in the GUI and injected into the LLM prompt",
    )
    podcast_behavior: str = Field(
        default="", description="Podcaster behavior instruction (empty = off)"
    )
    language: str = Field(default="", description="ISO 639-1 language code for the digest")
    voice: str = Field(default="", description="OmniVoice voice override")
    prompt_template: str = Field(
        default=DEFAULT_PROMPT_TEMPLATE,
        description="Prompt template name from src/core/prompts.py"
        "(e.g. 'default', 'finance', 'investigative')",
    )
    custom_prompt: str = Field(
        default="",
        description="Optional full custom system prompt that overrides prompt_template",
    )
    chat_id: str | None = Field(
        default=None,
        description="Telegram chat id override; global TELEGRAM_CHAT_ID if empty",
    )
    email_recipients: list[str] = Field(
        default_factory=list,
        description="Email recipients addressed per delivery; empty disables email",
    )
    whatsapp_recipients: list[str] = Field(
        default_factory=list,
        description=("WhatsApp recipients (E.164) addressed per delivery; empty disables WhatsApp"),
    )
    whatsapp_group_id: str | None = Field(
        default=None,
        description=("WhatsApp group id delivered as <id>@g.us, always exempt from the allowlist"),
    )
    delivery_channels: list[str] = Field(
        default_factory=lambda: ["telegram"],
        description=(
            "Delivery channels ('telegram', 'email', 'whatsapp'); empty or unset"
            " defaults to telegram"
        ),
    )
    logic_delivery: LogicDeliveryConfig | None = Field(
        default=None,
        description=(
            "Optional admin destination for the LLM news-selection logic; when"
            " absent the pipeline does not generate any logic text"
        ),
    )
    enabled: bool = Field(default=True, description="Pause a collection without deleting it")

    @field_validator("name")
    @classmethod
    def _name_required(cls, value: str) -> str:
        """Reject an empty collection name."""
        if not value.strip():
            raise ValueError("collection name must not be empty")
        return value.strip()

    @field_validator("time")
    @classmethod
    def _time_format(cls, value: str) -> str:
        """Enforce HH:MM (24-hour) format."""
        try:
            hours, minutes = (int(part) for part in value.split(":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("time must be in HH:MM format") from exc
        if len(value.split(":")) != 2 or not (0 <= hours <= 23 and 0 <= minutes <= 59):
            raise ValueError("time must be in HH:MM format")
        return f"{hours:02d}:{minutes:02d}"

    @field_validator("feed_urls")
    @classmethod
    def _feed_urls(cls, value: list[str]) -> list[str]:
        """Return only non-blank feed URLs."""
        return [url.strip() for url in value if url.strip()]

    @field_validator("delivery_channels")
    @classmethod
    def _delivery_channels(cls, value: list[str]) -> list[str]:
        """Keep only known channels, de-duplicated, falling back to telegram.

        Args:
            value: Raw channel list from the config.

        Returns:
            Ordered, de-duplicated channels; ['telegram'] when nothing valid.
        """
        known = ("telegram", "email", "whatsapp")
        channels = [channel for channel in known if channel in value]
        return channels or ["telegram"]

    @field_validator("whatsapp_recipients")
    @classmethod
    def _whatsapp_recipients(cls, value: list[str]) -> list[str]:
        """Reject non-E.164 numbers; an empty list still disables the channel."""
        clean = [num.strip() for num in value if num.strip()]
        invalid = [num for num in clean if not re.fullmatch(r"\+[1-9]\d{1,14}", num)]
        if invalid:
            raise ValueError(f"whatsapp_recipients must be E.164 numbers: {invalid}")
        return clean


def _parse_time(time_str: str) -> tuple[int, int]:
    """Split an HH:MM string into an (hour, minute) tuple.

    Args:
        time_str: Time in HH:MM format.

    Returns:
        Tuple of (hour, minute) integers.

    Raises:
        ValueError: If the string is not in HH:MM format.
    """
    hours, minutes = (int(part) for part in time_str.split(":"))
    return hours, minutes


class CollectionsLoader:
    """Loads and validates collections from a TOML file."""

    def __init__(self, path: str | None = None) -> None:
        """Initialize the loader with the collections file path.

        Args:
            path: Path to the collections.toml file. Defaults to settings.collections_file.
        """
        self.path = Path(path or get_settings().collections_file)

    def load(self) -> list[CollectionConfig]:
        """Parse the TOML file and build validated CollectionConfig objects.

        Returns:
            List of enabled CollectionConfig entries.

        Raises:
            FileNotFoundError: If the file does not exist.
            tomllib.TOMLDecodeError: If the file is not valid TOML.
            pydantic.ValidationError: If a collection entry is malformed.
        """
        if not self.path.exists():
            logger.info("collections_file_missing", path=str(self.path))
            return []

        import tomllib

        with self.path.open("rb") as fh:
            raw = tomllib.load(fh)

        defaults = raw.get("collection_defaults", {})
        raw_collections = raw.get("collections", [])
        collections: list[CollectionConfig] = []
        for idx, entry in enumerate(raw_collections):
            merged = {**defaults, **entry}
            merged.setdefault("name", f"collection-{idx + 1}")
            collections.append(CollectionConfig(**merged))
        logger.info("collections_loaded", path=str(self.path), count=len(collections))
        return collections

    def enabled(self) -> list[CollectionConfig]:
        """Return only the enabled collections.

        Returns:
            List of CollectionConfig entries with enabled=True.
        """
        return [c for c in self.load() if c.enabled]


@lru_cache
def load_collections(path: str | None = None) -> list[CollectionConfig]:
    """Cached loader for the collections file.

    Args:
        path: Optional path override for collections.toml.

    Returns:
        List of validated CollectionConfig entries.
    """
    return CollectionsLoader(path=path).load()


def get_collection(name: str, path: str | None = None) -> CollectionConfig | None:
    """Return a collection by name.

    Args:
        name: Collection name to look up.
        path: Optional path override for collections.toml.

    Returns:
        The matching CollectionConfig, or None if not found.
    """
    for collection in load_collections(path):
        if collection.name.casefold() == name.casefold():
            return collection
    return None


def default_collection() -> CollectionConfig:
    """Build the implicit single-collection config from global settings.

    Used when no collections.toml exists, preserving the pre-Phase-5 behavior.

    Returns:
        A CollectionConfig driven by settings.rss_feed_urls and TTS globals.
    """
    settings = get_settings()
    return CollectionConfig(
        name=DEFAULT_COLLECTION_NAME,
        feed_urls=settings.rss_feed_urls,
        time=DEFAULT_COLLECTION_TIME,
        tts_enabled=False,
        prompt_template=DEFAULT_PROMPT_TEMPLATE,
    )


def get_collections(path: str | None = None) -> list[CollectionConfig]:
    """Return collections to schedule: TOML-defined ones, or an implicit default.

    Args:
        path: Optional path override for collections.toml.

    Returns:
        List of collection configs to schedule.
    """
    collections = load_collections(path)
    if collections:
        return [c for c in collections if c.enabled]
    return [default_collection()]
