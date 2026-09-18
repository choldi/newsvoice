"""Unit tests for the collections-config monitor activities."""

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from temporalio.testing import ActivityEnvironment

from src.api.db.database import SyncStateRepository
from src.core.config import settings
from src.workers.activities import (
    COLLECTIONS_SIGNATURE_KEY,
    get_synced_signature_activity,
    read_collections_signature_activity,
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
    """Sync reconciles schedules, ensures the monitor, and stores the signature."""
    _write_collections(tmp_path)

    synced: dict = {}
    monitor_calls: list[object] = []

    class FakeAdmin:
        async def resync_schedules(self, collections: list) -> dict:
            synced["collections"] = collections
            return {"created_or_updated": ["daily-digest-tech"], "removed": []}

    async def fake_create_monitor_schedule(client: object) -> str:
        monitor_calls.append(client)
        return "collections-config-monitor"

    monkeypatch.setattr("src.api.services.temporal_admin.TemporalAdminService", FakeAdmin)
    monkeypatch.setattr(
        "src.workers.schedule.create_monitor_schedule", fake_create_monitor_schedule
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

    async def noop_monitor(client: object) -> str:
        return "collections-config-monitor"

    monkeypatch.setattr("src.api.services.temporal_admin.TemporalAdminService", FakeAdmin)
    monkeypatch.setattr("src.workers.schedule.create_monitor_schedule", noop_monitor)
    monkeypatch.setattr(
        "src.workers.run_worker.build_client",
        AsyncMock(return_value=object()),
    )

    await ActivityEnvironment().run(sync_collections_schedules_activity, "sig456")

    assert [c.name for c in synced["collections"]] == ["default"]
