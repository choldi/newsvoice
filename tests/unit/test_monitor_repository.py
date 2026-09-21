"""Unit tests for MonitorRepository (SQLite monitor event log)."""

from src.api.db.database import MonitorRepository, get_db


async def test_record_and_count_roundtrip() -> None:
    repo = MonitorRepository()
    await repo.record_event("edge_tts", "ok", "voices up")

    assert await repo.count() == 1


async def test_list_recent_orders_newest_first() -> None:
    repo = MonitorRepository()
    await repo.record_event("a", "ok", None)
    await repo.record_event("b", "error", "boom")

    rows = await repo.list_recent(limit=10)
    assert [r["check_name"] for r in rows] == ["b", "a"]
    assert rows[0]["status"] == "error"
    assert rows[0]["detail"] == "boom"


async def test_list_recent_respects_limit() -> None:
    repo = MonitorRepository()
    for i in range(5):
        await repo.record_event(f"check{i}", "ok", str(i))

    assert len(await repo.list_recent(limit=2)) == 2


async def test_purge_before_removes_only_old_rows() -> None:
    repo = MonitorRepository()
    await repo.record_event("fresh", "ok", None)
    async with get_db(None) as db:
        await db.execute(
            "INSERT INTO monitor_events (ts, check_name, status, detail) "
            "VALUES ('2020-01-01T00:00:00+00:00', 'old', 'ok', NULL)"
        )
        await db.commit()

    deleted = await repo.purge_before(retention_days=30)
    remaining = await repo.list_recent(limit=10)

    assert deleted == 1
    assert [r["check_name"] for r in remaining] == ["fresh"]


async def test_count_zero_on_empty_table() -> None:
    assert await MonitorRepository().count() == 0
