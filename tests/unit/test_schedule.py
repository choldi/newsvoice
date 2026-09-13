"""Unit tests for schedule helpers (per-collection Temporal schedules)."""

from src.core.collections import CollectionConfig
from src.workers.schedule import build_schedule, pipeline_config_for


def _collection(**overrides: object) -> CollectionConfig:
    """Build a minimal CollectionConfig for tests."""
    base: dict[str, object] = {
        "name": "tech",
        "feed_urls": ["https://example.com/rss"],
        "time": "08:00",
    }
    base.update(overrides)
    return CollectionConfig(**base)


def test_pipeline_config_for_maps_collection_fields() -> None:
    """PipelineConfig inherits feeds, TTS flag, template, chat."""
    collection = _collection(
        tts_enabled=True,
        deliver_audio=True,
        language="en",
        voice="en-gb-female",
        prompt_template="finance",
        chat_id="-100abcdef",
    )
    config = pipeline_config_for(collection, tts_flag=None)

    assert config.feed_urls == ["https://example.com/rss"]
    assert config.tts_enabled is True
    assert config.deliver_audio is True
    assert config.language == "en"
    assert config.voice == "en-gb-female"
    assert config.collection == "tech"
    assert config.prompt_template == "finance"
    assert config.chat_id == "-100abcdef"


def test_pipeline_config_for_tts_flag_overrides() -> None:
    """A CLI tts flag overrides the collection's tts_enabled value."""
    collection = _collection(tts_enabled=False)
    enabled = pipeline_config_for(collection, tts_flag=True)
    disabled = pipeline_config_for(collection, tts_flag=False)

    assert enabled.tts_enabled is True
    assert disabled.tts_enabled is False


def test_build_schedule_has_per_collection_ids() -> None:
    """Each collection gets its own schedule id and calendar time."""
    collection = _collection(time="17:15")
    schedule = build_schedule(collection, pipeline_config_for(collection, tts_flag=None))

    assert schedule.action.id == "digest-pipeline-tech"

    calendar = schedule.spec.calendars[0]
    assert calendar.hour.start == 17
    assert calendar.minute.start == 15
