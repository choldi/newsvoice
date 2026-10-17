"""Unit tests for schedule helpers (per-collection Temporal schedules)."""

from datetime import timedelta

import pytest

from src.api.models.monitoring import MonitorRunConfig
from src.core.collections import CollectionConfig
from src.core.config import settings
from src.workers.schedule import (
    MONITOR_SCHEDULE_ID,
    SYSTEM_MONITOR_SCHEDULE_ID,
    build_schedule,
    ensure_monitor_schedules,
    monitor_schedule,
    pipeline_config_for,
    system_monitor_schedule,
)


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
        email_recipients=["a@example.com"],
        delivery_channels=["telegram", "email"],
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
    assert config.email_recipients == ["a@example.com"]
    assert config.delivery_channels == ["telegram", "email"]


def test_pipeline_config_for_propagates_podcast_name() -> None:
    """PipelineConfig inherits the podcast_name from the collection."""
    collection = _collection(podcast_name="ByteCast")
    config = pipeline_config_for(collection, tts_flag=None)

    assert config.podcast_name == "ByteCast"


def test_pipeline_config_for_tts_flag_overrides() -> None:
    """A CLI tts flag overrides the collection's tts_enabled value."""
    collection = _collection(tts_enabled=False)
    enabled = pipeline_config_for(collection, tts_flag=True)
    disabled = pipeline_config_for(collection, tts_flag=False)

    assert enabled.tts_enabled is True
    assert disabled.tts_enabled is False


def _narration_spec(**overrides: object) -> dict[str, object]:
    """Build a minimal narration spec dict for tests."""
    payload: dict[str, object] = {
        "title": "Briefing",
        "roles": [{"name": "anchor", "voice_id": "es-mx-male"}],
    }
    payload.update(overrides)
    if str(payload.get("tts_backend", "")) in {"qwen3_tts", "zonos2"} and "roles" not in overrides:
        payload["roles"] = [{"name": "anchor", "voice_id": "anchor_neutral_m"}]
    return payload


def test_narration_routes_to_local_queue_for_omni_voice() -> None:
    collection = _collection(
        narration_enabled=True,
        narration_spec=_narration_spec(tts_backend="omni_voice"),
    )
    config = pipeline_config_for(collection, tts_flag=None)

    assert config.narration is not None
    assert config.narration["task_queue"] == settings.narration_local_task_queue


def test_narration_routes_to_modal_queue_for_qwen3_tts() -> None:
    collection = _collection(
        narration_enabled=True,
        narration_spec=_narration_spec(tts_backend="qwen3_tts"),
    )
    config = pipeline_config_for(collection, tts_flag=None)

    assert config.narration is not None
    assert config.narration["task_queue"] == settings.narration_modal_task_queue


def test_narration_routes_to_modal_queue_for_zonos2() -> None:
    collection = _collection(
        narration_enabled=True,
        narration_spec=_narration_spec(tts_backend="zonos2"),
    )
    config = pipeline_config_for(collection, tts_flag=None)

    assert config.narration is not None
    assert config.narration["task_queue"] == settings.narration_modal_task_queue


def test_narration_disabled_has_no_task_queue() -> None:
    collection = _collection(narration_enabled=False)
    config = pipeline_config_for(collection, tts_flag=None)

    assert config.narration is None


def test_build_schedule_has_per_collection_ids() -> None:
    """Each collection gets its own schedule id and calendar time."""
    collection = _collection(time="17:15")
    schedule = build_schedule(collection, pipeline_config_for(collection, tts_flag=None))

    assert schedule.action.id == "digest-pipeline-tech"

    calendar = schedule.spec.calendars[0]
    assert calendar.hour[0].start == 17
    assert calendar.minute[0].start == 15


def test_monitor_schedule_id_and_action() -> None:
    """The monitor schedule starts CollectionsMonitorWorkflow."""
    schedule = monitor_schedule()

    assert schedule.action.id == "collections-monitor"
    assert MONITOR_SCHEDULE_ID == "collections-config-monitor"


def test_monitor_schedule_uses_configured_interval() -> None:
    """The monitor wakes every COLLECTIONS_MONITOR_INTERVAL_MINUTES minutes."""
    schedule = monitor_schedule()
    expected = timedelta(minutes=settings.collections_monitor_interval_minutes)

    assert schedule.spec.intervals is not None
    assert schedule.spec.intervals[0].every == expected


def test_system_monitor_schedule_id_and_config() -> None:
    """The system-monitor schedule starts SystemMonitorWorkflow with config."""
    schedule = system_monitor_schedule()

    assert schedule.action.id == "system-monitor"
    assert SYSTEM_MONITOR_SCHEDULE_ID == "system-monitor"
    interval = schedule.spec.intervals[0].every
    assert interval == timedelta(minutes=settings.monitor_interval_minutes)
    assert schedule.action.args == [MonitorRunConfig(daily_summary=True, alert_on_run=False)]


async def test_ensure_monitor_schedules_creates_both(monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure_monitor_schedules returns both monitor schedule ids."""
    import src.workers.schedule as schedule_pkg

    async def fake_create_monitor(client: object) -> str:
        return MONITOR_SCHEDULE_ID

    async def fake_create_system(client: object) -> str:
        return SYSTEM_MONITOR_SCHEDULE_ID

    monkeypatch.setattr(schedule_pkg, "create_monitor_schedule", fake_create_monitor)
    monkeypatch.setattr(schedule_pkg, "create_system_monitor_schedule", fake_create_system)

    ids = await ensure_monitor_schedules(object())  # type: ignore[arg-type]

    assert ids == [MONITOR_SCHEDULE_ID, SYSTEM_MONITOR_SCHEDULE_ID]


# --- Phase 8.2: engine travels with the run ----------------------------------


def test_pipeline_config_carries_the_collection_backend() -> None:
    """The schedule payload must tell the activity which engine to use."""
    collection = _collection(tts_backend="omni_voice")

    config = pipeline_config_for(collection, tts_flag=None)

    assert config.tts_backend == "omni_voice"


def test_pipeline_config_backend_defaults_to_edge() -> None:
    """An unset collection backend keeps edge, whatever the engine default is."""
    collection = _collection()

    assert pipeline_config_for(collection, tts_flag=None).tts_backend == "edge"


def test_pipeline_config_backend_honours_the_global_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty collection backend inherits TTS_DEFAULT_BACKEND."""
    monkeypatch.setattr(settings, "tts_default_backend", "qwen3_tts")
    collection = _collection(tts_backend="")

    assert pipeline_config_for(collection, tts_flag=None).tts_backend == "qwen3_tts"
