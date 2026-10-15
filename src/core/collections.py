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
from urllib.parse import urlparse

import structlog
from pydantic import BaseModel, Field, field_validator, model_validator

from src.api.models.narration import NarrationSpec
from src.core.config import get_settings

logger = structlog.get_logger()

DEFAULT_COLLECTION_NAME = "default"
DEFAULT_COLLECTION_TIME = "08:00"
DEFAULT_PROMPT_TEMPLATE = "default"


class FeedSource(BaseModel):
    """One configured RSS feed with the reliability Phase 7.13 scores it with."""

    url: str = Field(..., min_length=1, description="Feed URL as configured")
    host: str = Field(default="", description="Lowercase host of the feed URL")
    reliability: float = Field(default=1.0, ge=0.0, le=1.0, description="Trust score in [0, 1]")
    hosts: list[str] = Field(
        default_factory=list,
        description="Extra article hosts this feed publishes under (aggregators)",
    )


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
    voice: str = Field(default="", description="Voice override, in the backend's vocabulary")
    tts_backend: str = Field(
        default="edge",
        description=(
            "Engine that synthesizes this collection's digest audio: 'edge', "
            "'omni_voice' (the tts-api gateway), 'qwen3_tts', 'zonos2' or "
            "'voxcpm2'. Empty falls back to TTS_DEFAULT_BACKEND."
        ),
    )
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
    narration_enabled: bool = Field(
        default=False, description="Generate narration audio for this collection"
    )
    narration_spec: NarrationSpec | None = Field(
        default=None, description="Narration spec (cast, music, defaults)"
    )
    narration_strategy: str = Field(
        default="turns", description="Render strategy: 'turns' or 'conversations'"
    )
    narration_deliver_audio: bool = Field(
        default=True, description="Deliver the narration audio to configured channels"
    )
    main_story_count: int = Field(
        default=5, ge=1, le=20, description="Number of main stories to include"
    )
    headline_count: int = Field(
        default=5, ge=1, le=40, description="Number of quick headlines to include"
    )
    # --- Phase 7.13: source reliability and LLM verification ---------------
    # Populated from inline tables in ``feed_urls``: a plain string keeps
    # ``source_reliability_default``, a table adds its own reliability and,
    # optionally, the extra hosts an aggregator publishes under.
    source_reliability: dict[str, float] = Field(
        default_factory=dict,
        description="Feed URL to reliability in [0, 1], from inline feed_urls tables",
    )
    source_hosts: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Feed URL to extra article hosts it publishes under (aggregators)",
    )
    llm_verify: bool = Field(
        default=False,
        description="Verify the generated digest against its sources before delivering",
    )
    llm_verify_threshold: float | None = Field(
        default=None,
        description="Faithfulness threshold for this collection; empty = global setting",
    )
    llm_verify_backend: str | None = Field(
        default=None,
        description="Judge backend for this collection: 'local', 'modal' or 'api'",
    )
    # --- Phase 7.14: audio verification (ASR) -------------------------------
    audio_verify: bool = Field(
        default=False,
        description="Transcribe the delivered audio and withhold it when the "
        "WER against the script exceeds the threshold",
    )
    audio_verify_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="WER ceiling for this collection; empty = global setting",
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

    @model_validator(mode="before")
    @classmethod
    def _split_inline_feed_tables(cls, value: object) -> object:
        """Split inline feed tables into plain URLs plus their reliability maps.

        ``feed_urls`` accepts two spellings so a collection can keep its list
        readable while still scoring its sources::

            feed_urls = [
              "https://elpais.com/rss",
              { url = "https://news.google.com/rss", reliability = 0.6,
                hosts = ["bbc.co.uk"] },
            ]

        The URLs stay a plain ``list[str]`` for every consumer (the RSS fetcher,
        the GUI textarea) and the extra keys are lifted into ``source_reliability``
        and ``source_hosts``. An explicit ``source_reliability`` table in the same
        document wins over the inline one.

        Args:
            value: Raw mapping of field names straight from the TOML document.

        Returns:
            The mapping with normalized feed entries, or the value untouched when
            it is not a mapping.
        """
        if not isinstance(value, dict):
            return value
        entries = value.get("feed_urls")
        if not isinstance(entries, list):
            return value
        urls: list[str] = []
        inline_reliability: dict[str, float] = {}
        inline_hosts: dict[str, list[str]] = {}
        for entry in entries:
            if isinstance(entry, str):
                url = entry.strip()
                if url:
                    urls.append(url)
                continue
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url") or "").strip()
            if not url:
                continue
            urls.append(url)
            reliability = entry.get("reliability")
            if reliability is not None:
                inline_reliability[url] = float(reliability)
            hosts = entry.get("hosts")
            if hosts:
                inline_hosts[url] = [str(host).strip() for host in hosts if str(host).strip()]
        merged = {**value, "feed_urls": urls}
        if inline_reliability:
            merged["source_reliability"] = {
                **inline_reliability,
                **(value.get("source_reliability") or {}),
            }
        if inline_hosts:
            merged["source_hosts"] = {**inline_hosts, **(value.get("source_hosts") or {})}
        return merged

    @field_validator("feed_urls")
    @classmethod
    def _feed_urls(cls, value: list[str]) -> list[str]:
        """Return only non-blank feed URLs."""
        return [url.strip() for url in value if url.strip()]

    @field_validator("source_reliability")
    @classmethod
    def _source_reliability(cls, value: dict[str, float]) -> dict[str, float]:
        """Normalize reliability keys and reject values outside [0, 1].

        Args:
            value: Raw reliability map keyed by feed URL.

        Returns:
            The map with trimmed, lowercase-free keys (URLs keep their case) and
            float values.

        Raises:
            ValueError: When any reliability falls outside [0, 1].
        """
        clean: dict[str, float] = {}
        for key, score in value.items():
            url = str(key).strip()
            if not url:
                continue
            score = float(score)
            if not 0.0 <= score <= 1.0:
                raise ValueError(f"source_reliability[{url}] must be within [0, 1]: {score}")
            clean[url] = score
        return clean

    @field_validator("source_hosts")
    @classmethod
    def _source_hosts(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        """Normalize the extra hosts declared for each feed.

        Args:
            value: Raw host map keyed by feed URL.

        Returns:
            The map with trimmed, lowercase, non-blank hosts.
        """
        clean: dict[str, list[str]] = {}
        for key, hosts in value.items():
            url = str(key).strip()
            if not url:
                continue
            normalized = [str(host).strip().lower() for host in hosts if str(host).strip()]
            if normalized:
                clean[url] = normalized
        return clean

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

    @field_validator("narration_strategy")
    @classmethod
    def _narration_strategy(cls, value: str) -> str:
        """Enforce allowed render strategies."""
        allowed = ("turns", "conversations")
        val = value.strip().lower()
        if val not in allowed:
            raise ValueError(f"narration_strategy must be one of {allowed}: {value!r}")
        return val

    @field_validator("tts_backend")
    @classmethod
    def _tts_backend_is_known(cls, value: str) -> str:
        """Accept only engines the digest synthesis can actually drive.

        A typo here would otherwise surface as a failed run at 08:00 instead of
        a rejected config save, so it is caught at load time.

        Args:
            value: Engine name from collections.toml.

        Returns:
            The canonical engine id; empty keeps TTS_DEFAULT_BACKEND.

        Raises:
            ValueError: If the name is not an engine news-engine can call.
        """
        from src.api.services.tts_backends import ALLOWED_BACKENDS, BACKEND_ALIASES

        raw = (value or "").strip().lower()
        if not raw:
            return ""
        canonical = BACKEND_ALIASES.get(raw, raw)
        if canonical not in ALLOWED_BACKENDS:
            raise ValueError(f"tts_backend must be one of {list(ALLOWED_BACKENDS)}: {value!r}")
        return canonical

    def resolved_feed_sources(self) -> list[FeedSource]:
        """Return each configured feed with its reliability and extra hosts.

        Returns:
            One FeedSource per feed URL, in configuration order.
        """
        sources: list[FeedSource] = []
        for url in self.feed_urls:
            host = (urlparse(url).hostname or "").lower()
            sources.append(
                FeedSource(
                    url=url,
                    host=host,
                    reliability=self.source_reliability.get(url, self.reliability_default()),
                    hosts=self.source_hosts.get(url, []),
                )
            )
        return sources

    def reliability_default(self) -> float:
        """Return the global default reliability for feeds without an explicit one.

        Returns:
            The configured default in [0, 1].
        """
        return get_settings().source_reliability_default

    def host_reliability(self) -> dict[str, float]:
        """Return the host to reliability map the verifier matches articles against.

        An article is attributed to the host of its own link, so a feed that
        republishes under other domains (``hosts``) contributes those domains too.

        Returns:
            Lowercase host to reliability.
        """
        mapping: dict[str, float] = {}
        for source in self.resolved_feed_sources():
            mapping[source.host] = source.reliability
            for host in source.hosts:
                mapping[host] = source.reliability
        return mapping

    def trusted_feed_urls(self) -> list[str]:
        """Return the feed URLs whose reliability clears the global floor.

        A source the operator does not trust is dropped before it reaches the
        pipeline: it must not be able to influence the digest at all. The
        collections signature keeps every URL so a change in an excluded feed
        still triggers a re-sync.

        Returns:
            Feed URLs in configuration order, excluding unreliable ones.
        """
        floor = get_settings().min_source_reliability
        return [
            url
            for url in self.feed_urls
            if self.source_reliability.get(url, self.reliability_default()) >= floor
        ]

    def unreliable_hosts(self) -> set[str]:
        """Return the hosts whose reliability falls below the global floor.

        These are dropped before clustering: a source the operator does not trust
        must not be able to influence the digest at all.

        Returns:
            Lowercase hosts to exclude.
        """
        floor = get_settings().min_source_reliability
        return {host for host, score in self.host_reliability().items() if score < floor}

    def resolve_llm_verify(self) -> dict[str, object] | None:
        """Return the Phase 7.13 verification payload for this collection.

        The kill switch and the per-collection opt-in are resolved **here**, outside
        the workflow, because a workflow cannot read settings. ``None`` means the
        collection does not verify, and the pipeline skips the check entirely.

        Returns:
            The verification payload, or None when verification is off.
        """
        settings = get_settings()
        if not settings.llm_verify_enabled or not self.llm_verify:
            return None
        threshold = (
            self.llm_verify_threshold
            if self.llm_verify_threshold is not None
            else settings.llm_verify_threshold
        )
        payload: dict[str, object] = {
            "enabled": True,
            "threshold": threshold,
            "groundedness_threshold": settings.llm_verify_groundedness_threshold,
            "backend": (self.llm_verify_backend or settings.llm_verify_backend).strip().lower(),
            "model": settings.llm_verify_model,
            "host_reliability": self.host_reliability(),
            "min_reliability": settings.min_source_reliability,
        }
        return payload

    def resolve_audio_verify(self) -> dict[str, object] | None:
        """Return the Phase 7.14 audio-verification payload for this collection.

        Resolved outside the workflow (a workflow cannot read settings), like
        ``resolve_llm_verify``. ``None`` means the collection does not verify
        its audio and the gate is skipped.

        Returns:
            The payload (enabled + threshold), or None when audio verification
            is off for this collection.
        """
        settings = get_settings()
        if not settings.audio_verify_enabled or not self.audio_verify:
            return None
        threshold = (
            self.audio_verify_threshold
            if self.audio_verify_threshold is not None
            else settings.audio_verify_wer_threshold
        )
        return {"enabled": True, "threshold": threshold}


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
