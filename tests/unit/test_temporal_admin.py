"""Unit tests for the Temporal schedule admin service."""

from unittest.mock import AsyncMock, patch

import pytest
from temporalio.client import RPCError
from temporalio.service import RPCStatusCode

from src.api.services.temporal_admin import TemporalAdminService
from src.core.collections import CollectionConfig
from src.workers.schedule import pipeline_config_for


class FakeHandle:
    """Minimal ScheduleHandle stand-in."""

    def __init__(self, schedule_id: str, not_found: bool = False) -> None:
        self.id = schedule_id
        self._not_found = not_found
        self.updated = 0
        self.deleted = False

    async def describe(self) -> None:
        if self._not_found:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")

    async def update(self, schedule: object) -> None:
        self.updated += 1

    async def delete(self) -> None:
        self.deleted = True


class FakeClient:
    """Fake Temporal client supporting list/create/update/delete schedules."""

    def __init__(self, existing: list[str] | None = None) -> None:
        self.schedules: dict[str, FakeHandle] = {
            schedule_id: FakeHandle(schedule_id) for schedule_id in (existing or [])
        }
        self.created: list[str] = []

    async def list_schedules(self):  # type: ignore[override]
        for handle in list(self.schedules.values()):
            yield handle

    async def get_schedule_handle(self, schedule_id: str) -> FakeHandle:
        if schedule_id not in self.schedules:
            return FakeHandle(schedule_id, not_found=True)
        return self.schedules[schedule_id]

    async def create_schedule(self, schedule_id: str, schedule: object) -> FakeHandle:
        handle = FakeHandle(schedule_id)
        self.schedules[schedule_id] = handle
        self.created.append(schedule_id)
        return handle


def _tech(name: str = "tech") -> CollectionConfig:
    return CollectionConfig(
        name=name,
        time="08:00",
        feed_urls=["https://techcrunch.com/feed/"],
        podcast_name="Tech Daily",
        enabled=True,
    )


@pytest.mark.asyncio
async def test_pipeline_config_propagates_podcast_name() -> None:
    """pipeline_config_for carries podcast_name into the workflow config."""
    config = pipeline_config_for(_tech(), tts_flag=None)
    assert config.podcast_name == "Tech Daily"
    assert config.collection == "tech"


@pytest.mark.asyncio
async def test_resync_creates_new_and_removes_stale() -> None:
    """New collections get schedules; stale ones are deleted."""
    fake = FakeClient(existing=["daily-digest-removed"])
    service = TemporalAdminService(client=fake)  # type: ignore[arg-type]

    result = await service.resync_schedules([_tech(), _tech("finance")])

    assert "daily-digest-tech" in result["created_or_updated"]
    assert "daily-digest-finance" in result["created_or_updated"]
    assert result["removed"] == ["daily-digest-removed"]
    assert fake.schedules["daily-digest-tech"].updated == 0  # created, not updated


@pytest.mark.asyncio
async def test_resync_updates_existing_and_skips_disabled() -> None:
    """Existing schedules are updated; disabled collections are not scheduled."""
    fake = FakeClient(existing=["daily-digest-tech"])
    service = TemporalAdminService(client=fake)  # type: ignore[arg-type]

    enabled = _tech("tech").model_copy(update={"time": "12:30", "enabled": True})
    other = _tech("health").model_copy(update={"enabled": False})

    await service.resync_schedules([enabled, other])

    assert fake.schedules["daily-digest-tech"].updated == 1
    assert "daily-digest-health" not in fake.schedules


@pytest.mark.asyncio
async def test_uses_build_client_when_no_client_given() -> None:
    """Without an injected client, the service connects via build_client."""
    fake = FakeClient()
    with patch(
        "src.api.services.temporal_admin.build_client",
        new_callable=AsyncMock,
        return_value=fake,
    ) as mock_build:
        service = TemporalAdminService()
        await service.resync_schedules([_tech()])
        mock_build.assert_awaited_once()
    assert "daily-digest-tech" in fake.created
