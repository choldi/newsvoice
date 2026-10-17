"""Unit tests for the collections-config monitor activities."""

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from temporalio.testing import ActivityEnvironment

from src.api.db.database import MonitorRepository, SyncStateRepository
from src.api.models.monitoring import CheckResult
from src.core.config import settings
from src.workers.activities import (
    COLLECTIONS_NAMES_KEY,
    COLLECTIONS_SIGNATURE_KEY,
    collections_changes_activity,
    get_monitor_state_activity,
    get_synced_signature_activity,
    read_collections_signature_activity,
    run_monitor_checks_activity,
    send_monitor_alert_activity,
    set_monitor_state_activity,
    sync_collections_schedules_activity,
)

COLLECTIONS_TOML = """
[[collections]]
name = 'tech'
time = '08:00'
feed_urls = ['https://example.com/rss']

[[collections]]
name = 'health'
time = '09:30'
feed_urls = ['https://health.example.com/rss']
"""


def _write_collections(tmp_path: Path, content: str = COLLECTIONS_TOML) -> Path:
    """Write collections.toml to a temp path and point settings at it."""
    path = tmp_path / "collections.toml"
    path.write_text(content, encoding="utf-8")
    settings.collections_file = str(path)
    return path


async def test_read_collections_signature_returns_sha256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid file returns the sha256 of its bytes."""
    path = _write_collections(tmp_path)
    expected = hashlib.sha256(path.read_bytes()).hexdigest()

    sig = await ActivityEnvironment().run(read_collections_signature_activity)

    assert sig == expected


async def test_read_collections_signature_missing_file_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing file yields None so the monitor skips syncing."""
    settings.collections_file = "/does/not/exist.toml"

    sig = await ActivityEnvironment().run(read_collections_signature_activity)

    assert sig is None


async def test_read_collections_signature_invalid_toml_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed file yields None (drift is not applied until it parses)."""
    _write_collections(tmp_path, "not = [valid toml")

    sig = await ActivityEnvironment().run(read_collections_signature_activity)

    assert sig is None


async def test_get_synced_signature_activity_roundtrip() -> None:
    """The KV store is read through the activity."""
    assert await ActivityEnvironment().run(get_synced_signature_activity) is None

    await SyncStateRepository().set(COLLECTIONS_SIGNATURE_KEY, "abc123")

    assert await ActivityEnvironment().run(get_synced_signature_activity) == "abc123"


async def test_sync_collections_schedules_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sync reconciles schedules, ensures the monitors, and stores the signature."""
    _write_collections(tmp_path)

    synced: dict = {}
    monitor_calls: list[object] = []

    class FakeAdmin:
        async def resync_schedules(self, collections: list) -> dict:
            synced["collections"] = collections
            return {"created_or_updated": ["daily-digest-tech"], "removed": []}

    async def fake_ensure_monitor_schedules(client: object) -> list[str]:
        monitor_calls.append(client)
        return ["collections-config-monitor", "system-monitor"]

    monkeypatch.setattr("src.api.services.temporal_admin.TemporalAdminService", FakeAdmin)
    monkeypatch.setattr(
        "src.workers.schedule.ensure_monitor_schedules", fake_ensure_monitor_schedules
    )
    monkeypatch.setattr(
        "src.workers.run_worker.build_client",
        AsyncMock(return_value=object()),
    )

    await ActivityEnvironment().run(sync_collections_schedules_activity, "sig123")

    names = {c.name for c in synced["collections"]}
    assert names == {"tech", "health"}
    assert len(monitor_calls) == 1
    assert await SyncStateRepository().get(COLLECTIONS_SIGNATURE_KEY) == "sig123"


async def test_sync_collections_schedules_stores_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sync persists the enabled collection names for the diff alert."""
    synced: dict = {}

    class FakeAdmin:
        async def resync_schedules(self, collections: list) -> dict:
            synced["collections"] = collections
            return {"created_or_updated": [], "removed": []}

    async def fake_ensure_monitor_schedules(client: object) -> list[str]:
        return ["collections-config-monitor", "system-monitor"]

    monkeypatch.setattr("src.api.services.temporal_admin.TemporalAdminService", FakeAdmin)
    monkeypatch.setattr(
        "src.workers.schedule.ensure_monitor_schedules", fake_ensure_monitor_schedules
    )
    monkeypatch.setattr(
        "src.workers.run_worker.build_client",
        AsyncMock(return_value=object()),
    )

    await ActivityEnvironment().run(sync_collections_schedules_activity, "sig456")

    stored = await SyncStateRepository().get(COLLECTIONS_NAMES_KEY)
    assert stored == '["health", "tech"]'


async def test_collections_changes_activity_detects_added_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The diff reports collections added/removed versus the stored snapshot."""
    _write_collections(tmp_path)
    await SyncStateRepository().set(COLLECTIONS_NAMES_KEY, '["tech", "finance"]')

    changes = await ActivityEnvironment().run(collections_changes_activity)

    assert changes["added"] == ["health"]
    assert changes["removed"] == ["finance"]


async def test_collections_changes_activity_no_snapshot_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a stored snapshot the diff is empty (bootstrap has no alert)."""
    _write_collections(tmp_path)

    changes = await ActivityEnvironment().run(collections_changes_activity)

    assert changes == {"added": [], "removed": []}


async def test_sync_collections_schedules_activity_default_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty collections file falls back to the implicit default collection."""
    _write_collections(tmp_path, "[[collections]]\nname = 'temp'\nenabled = false\n")

    synced: dict = {}

    class FakeAdmin:
        async def resync_schedules(self, collections: list) -> dict:
            synced["collections"] = collections
            return {"created_or_updated": [], "removed": []}

    async def noop_monitor(client: object) -> list[str]:
        return ["collections-config-monitor", "system-monitor"]

    monkeypatch.setattr("src.api.services.temporal_admin.TemporalAdminService", FakeAdmin)
    monkeypatch.setattr("src.workers.schedule.ensure_monitor_schedules", noop_monitor)
    monkeypatch.setattr(
        "src.workers.run_worker.build_client",
        AsyncMock(return_value=object()),
    )

    await ActivityEnvironment().run(sync_collections_schedules_activity, "sig456")

    assert [c.name for c in synced["collections"]] == ["default"]


async def test_get_set_monitor_state_roundtrip() -> None:
    """The KV state helpers persist and read back values."""
    await set_monitor_state_activity("monitor:last_summary_day", "2026-09-21")

    assert await get_monitor_state_activity("monitor:last_summary_day") == "2026-09-21"


async def test_get_monitor_state_missing_returns_none() -> None:
    assert await get_monitor_state_activity("monitor:does_not_exist") is None


async def test_run_monitor_checks_activity_persists_and_alerts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The activity runs the registry, persists outcomes, and flags transitions."""

    async def fake_checks() -> list[CheckResult]:
        return [CheckResult(check="edge_tts", status="error", detail="backend down")]

    monkeypatch.setattr("src.api.services.monitoring.checks.run_all_checks", fake_checks)
    monkeypatch.setattr(settings, "monitor_log_dir", str(tmp_path / "logs"))
    monkeypatch.setattr(settings, "monitor_retention_days", 3)

    report = await ActivityEnvironment().run(
        run_monitor_checks_activity, {"daily_summary": True, "alert_on_run": False}
    )

    assert report["checks"][0]["check"] == "edge_tts"
    assert report["alert"][0]["check"] == "edge_tts"
    assert await MonitorRepository().count() == 1


async def test_run_monitor_checks_activity_alert_on_run_appends_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """alert_on_run appends an info notification for newly completed runs."""
    from src.api.services.monitoring.reporter import MonitorReporter

    async def fake_checks() -> list[CheckResult]:
        return [CheckResult(check="disk_local", status="ok")]

    async def fake_new_runs(self: object) -> list[str]:
        return ["tech: ok (5 articles, audio=y)"]

    monkeypatch.setattr("src.api.services.monitoring.checks.run_all_checks", fake_checks)
    monkeypatch.setattr(MonitorReporter, "new_completed_runs", fake_new_runs)
    monkeypatch.setattr(settings, "monitor_log_dir", str(tmp_path / "logs"))
    monkeypatch.setattr(settings, "monitor_retention_days", 3)

    report = await ActivityEnvironment().run(
        run_monitor_checks_activity, {"daily_summary": True, "alert_on_run": True}
    )

    alerts = [a["check"] for a in report["alert"]]
    assert "digest_runs" in alerts
    info = next(a for a in report["alert"] if a["check"] == "digest_runs")
    assert info["status"] == "info"


async def test_send_monitor_alert_telegram_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured Telegram channel yields a successful result."""
    from src.workers import activities as activities_pkg

    async def fake_deliver(text: str, chat_id: str) -> dict:
        assert "News-engine alert" in text
        assert chat_id == settings.monitor_telegram_chat_id or chat_id == settings.telegram_chat_id
        return {"message_id": 42}

    monkeypatch.setattr(activities_pkg, "deliver_telegram_message_activity", fake_deliver)
    monkeypatch.setattr(settings, "monitor_notify_channels", ["telegram"])
    monkeypatch.setattr(settings, "monitor_telegram_chat_id", "monitor-chat")

    outcome = await ActivityEnvironment().run(send_monitor_alert_activity, "News-engine alert test")

    assert outcome["status"] == "ok"
    assert outcome["results"][0]["channel"] == "telegram"
    assert outcome["results"][0]["message_id"] == 42


async def test_send_monitor_alert_channel_error_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing channel is recorded as an error without aborting others."""
    from src.workers import activities as activities_pkg

    async def failing_deliver(text: str, chat_id: str) -> dict:
        raise RuntimeError("telegram down")

    monkeypatch.setattr(activities_pkg, "deliver_telegram_message_activity", failing_deliver)
    monkeypatch.setattr(settings, "monitor_notify_channels", ["telegram"])
    monkeypatch.setattr(settings, "monitor_telegram_chat_id", "monitor-chat")

    outcome = await ActivityEnvironment().run(send_monitor_alert_activity, "nope")

    assert outcome["status"] == "error"
    assert outcome["results"][0]["error"] == "telegram down"


async def test_send_monitor_alert_unknown_channel_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown channels are skipped, not raised."""
    monkeypatch.setattr(settings, "monitor_notify_channels", ["non_existent_channel"])

    outcome = await ActivityEnvironment().run(send_monitor_alert_activity, "x")

    assert outcome["status"] == "ok"
    assert outcome["results"][0]["status"] == "skipped"
    assert outcome["results"][0]["reason"] == "unknown channel"
