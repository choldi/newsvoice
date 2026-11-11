"""Unit tests for the collections.toml loader and helpers."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.core.collections import (
    CollectionConfig,
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


def test_empty_delivery_channels_disables_delivery(tmp_path: Path) -> None:
    """An explicit empty delivery_channels list disables delivery."""
    path = _write_collections(
        tmp_path,
        '[[collections]]\nname = "tech"\nfeed_urls = ["https://x.com/rss"]\n'
        "delivery_channels = []\n",
    )
    collection = CollectionsLoader(str(path)).load()[0]
    assert collection.delivery_channels == []


def test_unknown_delivery_channels_fall_back_to_telegram(tmp_path: Path) -> None:
    """A non-empty list with only unknown channels keeps legacy telegram delivery."""
    path = _write_collections(
        tmp_path,
        '[[collections]]\nname = "tech"\nfeed_urls = ["https://x.com/rss"]\n'
        'delivery_channels = ["sms"]\n',
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


def test_example_gaming_toml_parses_without_null_music(tmp_path: Path) -> None:
    """Regression test: the gaming example TOML must not contain `music = null`
    (invalid TOML) and must parse into a valid CollectionConfig with all
    Phase 7.6 fields present."""
    # Valid TOML string. Inline table must be single-line per TOML spec.
    gaming_toml = (
        "[collection_defaults]\n"
        "tts_enabled = false\n"
        "deliver_audio = false\n"
        'language = "en"\n'
        'voice = ""\n'
        "narration_enabled = false\n"
        'narration_strategy = "turns"\n'
        "narration_deliver_audio = true\n"
        "main_story_count = 5\n"
        "headline_count = 5\n\n"
        "[[collections]]\n"
        'name = "gaming"\n'
        'time = "10:00"\n'
        "enabled = true\n"
        "feed_urls = [\n"
        '    "https://feeds.feedburner.com/ign/all",\n'
        '    "https://www.polygon.com/rss/index.xml",\n'
        '    "https://kotaku.com/rss",\n'
        '    "https://www.vg247.com/feed",\n'
        "]\n"
        'language = "en"\n'
        "main_story_count = 5\n"
        "headline_count = 10\n"
        "tts_enabled = false\n"
        "deliver_audio = false\n"
        'podcast_name = "Game Over"\n'
        'podcast_behavior = "Dos co-hosts: Michael (entusiasta, conduce) y Dwight '
        "(analista escéptico, deadpan). Diálogo natural, turnos cortos, "
        "terminología gamer (patch, meta, nerf, speedrun, NPC, grind). Michael "
        "abre con 'Welcome to Game Over, your daily gaming briefing.' Dwight "
        "aporta contexto técnico y escepticismo. Titulares: ida y vuelta rápida, "
        'una frase por item."\n'
        'prompt_template = "gaming"\n'
        "narration_enabled = true\n"
        'narration_strategy = "turns"\n'
        "narration_deliver_audio = true\n"
        'narration_spec = { title = "Game Over", language = "en", roles = ['
        '{name="michael", voice_id="3e47fbcc-53a8-4551-be91-48f93d62a19f", '
        'speed=1.0, emotion="excited"}, '
        '{name="dwight", voice_id="f0c280e3-0b2d-4f91-9ad6-ff0f86cf49f8", '
        'speed=1.0, emotion="calm"}], pause_ms = 350 }\n'
        'delivery_channels = ["telegram"]\n'
    )
    path = _write_collections(tmp_path, gaming_toml)
    loader = CollectionsLoader(str(path))
    collections = loader.load()

    assert len(collections) == 1
    gaming = collections[0]
    assert gaming.name == "gaming"
    assert gaming.main_story_count == 5
    assert gaming.headline_count == 10
    assert gaming.narration_enabled is True
    assert gaming.narration_deliver_audio is True
    assert gaming.prompt_template == "gaming"
    assert gaming.narration_spec is not None
    assert gaming.narration_spec.title == "Game Over"
    assert len(gaming.narration_spec.roles) == 2
    role_names = {r.name for r in gaming.narration_spec.roles}
    assert role_names == {"michael", "dwight"}
    # music must be None (omitted in TOML, not "null")
    assert gaming.narration_spec.music is None
    assert gaming.delivery_channels == ["telegram"]


def test_example_ai_toml_parses_with_a_conversations_spec(tmp_path: Path) -> None:
    """The AI Pulse example TOML parses into a conversations-ready spec.

    The conversations strategy rejects per-role emotions and heterogeneous
    speeds, so the example must carry neither: one global speed only.
    """
    ai_toml = (
        "[[collections]]\n"
        'name = "ai"\n'
        'time = "11:00"\n'
        "enabled = false\n"
        "feed_urls = [\n"
        '    "https://techcrunch.com/category/artificial-intelligence/feed/",\n'
        '    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",\n'
        '    "https://www.wired.com/feed/tag/ai/latest/rss",\n'
        '    "https://www.marktechpost.com/feed/",\n'
        '    "https://export.arxiv.org/rss/stat.ML",\n'
        "]\n"
        'language = "en"\n'
        "main_story_count = 5\n"
        "headline_count = 8\n"
        "tts_enabled = false\n"
        "deliver_audio = false\n"
        'podcast_name = "AI Pulse"\n'
        'podcast_behavior = "Two co-hosts: Michael and Dwight."\n'
        'prompt_template = "ai"\n'
        "narration_enabled = true\n"
        'narration_strategy = "conversations"\n'
        "narration_deliver_audio = true\n"
        'narration_spec = { title = "AI Pulse", language = "en", roles = ['
        '{name="michael", voice_id="3e47fbcc-53a8-4551-be91-48f93d62a19f"}, '
        '{name="dwight", voice_id="f0c280e3-0b2d-4f91-9ad6-ff0f86cf49f8"}], '
        "pause_ms = 300, speed = 1.1 }\n"
        'delivery_channels = ["telegram"]\n'
    )
    path = _write_collections(tmp_path, ai_toml)
    collections = CollectionsLoader(str(path)).load()

    assert len(collections) == 1
    ai = collections[0]
    assert ai.name == "ai"
    assert ai.time == "11:00"
    assert ai.enabled is False
    assert ai.language == "en"
    assert ai.prompt_template == "ai"
    assert ai.narration_strategy == "conversations"
    assert ai.narration_spec is not None
    assert ai.narration_spec.speed == 1.1
    assert ai.narration_spec.emotion is None
    assert len(ai.narration_spec.roles) == 2
    assert {r.name for r in ai.narration_spec.roles} == {"michael", "dwight"}
    assert all(r.emotion is None for r in ai.narration_spec.roles)
    assert all(r.speed == 1.0 for r in ai.narration_spec.roles)
    assert ai.delivery_channels == ["telegram"]


# ---------------------------------------------------------------------------
# Phase 7.13: source reliability and LLM verification
# ---------------------------------------------------------------------------


def test_inline_feed_table_splits_into_plain_urls_and_reliability(tmp_path: Path) -> None:
    """Inline feed tables are decomposed into feed_urls + source_reliability + source_hosts."""
    content = """
[[collections]]
name = "ai"
feed_urls = [
  "https://elpais.com/rss",
  { url = "https://news.google.com/rss", reliability = 0.6, hosts = ["bbc.co.uk"] },
  { url = "https://low.trust/rss", reliability = 0.2 },
]
"""
    path = tmp_path / "collections.toml"
    path.write_text(content)
    collections = CollectionsLoader(str(path)).load()

    assert len(collections) == 1
    ai = collections[0]
    assert ai.feed_urls == [
        "https://elpais.com/rss",
        "https://news.google.com/rss",
        "https://low.trust/rss",
    ]
    assert ai.source_reliability == {
        "https://news.google.com/rss": 0.6,
        "https://low.trust/rss": 0.2,
    }
    assert ai.source_hosts == {"https://news.google.com/rss": ["bbc.co.uk"]}


def test_explicit_source_reliability_table_merges_with_inline(tmp_path: Path) -> None:
    """An explicit source_reliability table is merged with inline table entries."""
    content = """
[[collections]]
name = "ai"
feed_urls = [
  "https://elpais.com/rss",
  { url = "https://news.google.com/rss", reliability = 0.6 },
]
source_reliability = { "https://elpais.com/rss" = 0.95 }
"""
    path = tmp_path / "collections.toml"
    path.write_text(content)
    collections = CollectionsLoader(str(path)).load()

    ai = collections[0]
    assert ai.source_reliability == {
        "https://news.google.com/rss": 0.6,
        "https://elpais.com/rss": 0.95,
    }


def test_source_reliability_rejects_out_of_range_values(tmp_path: Path) -> None:
    """Reliability scores outside [0, 1] are rejected."""
    content = """
[[collections]]
name = "ai"
feed_urls = [
  { url = "https://bad.com/rss", reliability = 1.5 },
]
"""
    path = tmp_path / "collections.toml"
    path.write_text(content)
    with pytest.raises(Exception):
        CollectionsLoader(str(path)).load()


def test_host_reliability_map_includes_extra_hosts() -> None:
    """host_reliability() maps both the feed host and its extra hosts."""
    config = CollectionConfig.model_validate(
        {
            "name": "ai",
            "feed_urls": [
                "https://news.google.com/rss",
            ],
            "source_reliability": {"https://news.google.com/rss": 0.6},
            "source_hosts": {"https://news.google.com/rss": ["bbc.co.uk", "cnn.com"]},
        }
    )
    mapping = config.host_reliability()
    assert mapping["news.google.com"] == 0.6
    assert mapping["bbc.co.uk"] == 0.6
    assert mapping["cnn.com"] == 0.6


def test_unreliable_hosts_below_floor() -> None:
    """Hosts with reliability below min_source_reliability are flagged."""
    config = CollectionConfig.model_validate(
        {
            "name": "ai",
            "feed_urls": [
                "https://trusted.com/rss",
                "https://low.trust/rss",
            ],
            "source_reliability": {"https://low.trust/rss": 0.2},
        }
    )
    assert config.unreliable_hosts() == {"low.trust"}


def test_trusted_feed_urls_excludes_unreliable() -> None:
    """trusted_feed_urls() drops feeds below the reliability floor."""
    config = CollectionConfig.model_validate(
        {
            "name": "ai",
            "feed_urls": [
                "https://trusted.com/rss",
                "https://low.trust/rss",
            ],
            "source_reliability": {"https://low.trust/rss": 0.2},
        }
    )
    assert config.trusted_feed_urls() == ["https://trusted.com/rss"]


def test_resolve_llm_verify_returns_none_when_disabled() -> None:
    """resolve_llm_verify() returns None when the collection does not opt in."""
    config = CollectionConfig(name="ai", feed_urls=["https://a.com/rss"])
    assert config.resolve_llm_verify() is None


def test_resolve_llm_verify_returns_payload_when_enabled() -> None:
    """resolve_llm_verify() returns the full payload when opted in."""
    config = CollectionConfig.model_validate(
        {
            "name": "ai",
            "feed_urls": ["https://a.com/rss"],
            "llm_verify": True,
            "llm_verify_threshold": 0.85,
            "llm_verify_backend": "modal",
        }
    )
    payload = config.resolve_llm_verify()
    assert payload is not None
    assert payload["enabled"] is True
    assert payload["threshold"] == 0.85
    assert payload["backend"] == "modal"
    assert payload["host_reliability"] == {"a.com": 1.0}


def test_resolve_llm_verify_uses_global_threshold_when_not_set() -> None:
    """A collection without its own threshold falls back to the global setting."""
    config = CollectionConfig.model_validate(
        {
            "name": "ai",
            "feed_urls": ["https://a.com/rss"],
            "llm_verify": True,
        }
    )
    payload = config.resolve_llm_verify()
    assert payload is not None
    assert payload["threshold"] == 0.9  # global default


# ---------------------------------------------------------------------------
# Phase 7.14: audio verification (opt-in)
# ---------------------------------------------------------------------------


def test_audio_verify_is_opt_in() -> None:
    """By default a collection does not verify its audio."""
    config = CollectionConfig(name="ai", feed_urls=["https://a.com/rss"])
    assert config.resolve_audio_verify() is None


def test_audio_verify_uses_the_global_threshold() -> None:
    """Opting in without a threshold inherits audio_verify_wer_threshold."""
    config = CollectionConfig(name="ai", feed_urls=["https://a.com/rss"], audio_verify=True)
    payload = config.resolve_audio_verify()
    assert payload is not None
    assert payload["enabled"] is True
    assert payload["threshold"] == 0.30


def test_audio_verify_honours_a_collection_threshold() -> None:
    """A per-collection WER ceiling overrides the global one."""
    config = CollectionConfig(
        name="ai",
        feed_urls=["https://a.com/rss"],
        audio_verify=True,
        audio_verify_threshold=0.15,
    )
    payload = config.resolve_audio_verify()
    assert payload is not None
    assert payload["threshold"] == 0.15


def test_audio_verify_respects_the_global_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audio_verify_enabled=false turns every collection off at once."""
    monkeypatch.setattr("src.core.config.settings.audio_verify_enabled", False)
    config = CollectionConfig(name="ai", feed_urls=["https://a.com/rss"], audio_verify=True)
    assert config.resolve_audio_verify() is None


def test_audio_verify_threshold_is_validated() -> None:
    """A WER ceiling outside [0, 1] is a configuration error."""
    with pytest.raises(ValidationError):
        CollectionConfig(
            name="ai",
            feed_urls=["https://a.com/rss"],
            audio_verify=True,
            audio_verify_threshold=1.5,
        )


# --- Phase 8.2: per-collection TTS engine ------------------------------------


def test_tts_backend_defaults_to_edge() -> None:
    """A collection that says nothing keeps the current behaviour."""
    collection = CollectionConfig(name="tech", feed_urls=["https://x/rss"])

    assert collection.tts_backend == "edge"


def test_tts_backend_accepts_every_engine_and_canonicalizes_aliases() -> None:
    """Operator spellings of the gateway collapse onto one canonical id."""
    assert CollectionConfig(name="a", tts_backend="omni_voice").tts_backend == "omni_voice"
    assert CollectionConfig(name="b", tts_backend="gateway").tts_backend == "omni_voice"
    assert CollectionConfig(name="c", tts_backend="QWEN3_TTS").tts_backend == "qwen3_tts"
    assert CollectionConfig(name="d", tts_backend="zonos2").tts_backend == "zonos2"
    assert CollectionConfig(name="e", tts_backend="edgetts").tts_backend == "edge"


def test_tts_backend_empty_falls_back_to_the_global_default() -> None:
    """An empty value is allowed so a shared default can be inherited."""
    collection = CollectionConfig(name="tech", tts_backend="")

    assert collection.tts_backend == ""


def test_tts_backend_rejects_an_unknown_engine() -> None:
    """A typo fails when the file is loaded, not at 08:00 during a run."""
    with pytest.raises(ValidationError, match="tts_backend must be one of"):
        CollectionConfig(name="tech", tts_backend="siri")
