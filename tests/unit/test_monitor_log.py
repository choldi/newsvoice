"""Unit tests for MonitorLogStore (daily rotating JSONL)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.api.services.monitor_log import MonitorLogStore


def _day_str(offset: int) -> str:
    return (datetime.now(UTC) - timedelta(days=offset)).strftime("%Y-%m-%d")


async def test_append_writes_todays_jsonl(tmp_path: Path) -> None:
    store = MonitorLogStore(log_dir=str(tmp_path), retention_days=3)
    await store.append("2026-09-21T10:00:00+00:00", "edge_tts", "ok", "fine")

    files = list(tmp_path.glob("monitor_*.jsonl"))
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record == {
        "ts": "2026-09-21T10:00:00+00:00",
        "check": "edge_tts",
        "status": "ok",
        "detail": "fine",
    }


async def test_last_days_reads_back_multiple_check_lines(tmp_path: Path) -> None:
    store = MonitorLogStore(log_dir=str(tmp_path), retention_days=3)
    await store.append("2026-09-21T10:00:00+00:00", "services", "warning", "api down")
    await store.append("2026-09-21T10:01:00+00:00", "edge_tts", "ok", None)

    records = await store.last_days(days=1)
    assert [r["check"] for r in records] == ["services", "edge_tts"]
    assert records[0]["detail"] == "api down"


async def test_purge_expired_deletes_old_daily_files(tmp_path: Path) -> None:
    old = tmp_path / f"monitor_{_day_str(10)}.jsonl"
    old.write_text('{"ts":"x"}\n', encoding="utf-8")
    now = tmp_path / f"monitor_{_day_str(0)}.jsonl"
    now.write_text('{"ts":"y"}\n', encoding="utf-8")

    store = MonitorLogStore(log_dir=str(tmp_path), retention_days=3)
    await store.append("2026-09-21T11:00:00+00:00", "disk", "ok", None)

    remaining = [p.name for p in tmp_path.glob("monitor_*.jsonl")]
    assert old.name not in remaining
    assert now.name in remaining


async def test_last_days_ignores_corrupt_lines(tmp_path: Path) -> None:
    store = MonitorLogStore(log_dir=str(tmp_path), retention_days=3)
    today = store._today_path()
    (tmp_path / today.name).write_text("not-json\n", encoding="utf-8")
    await store.append("2026-09-21T12:00:00+00:00", "tts_disk", "ok", None)

    records = await store.last_days(days=1)
    assert [r["check"] for r in records] == ["tts_disk"]


async def test_append_lazy_creates_dir(tmp_path: Path) -> None:
    store = MonitorLogStore(log_dir=str(tmp_path / "nested" / "logs"), retention_days=3)
    await store.append("2026-09-21T12:00:00+00:00", "a", "ok", None)

    assert (tmp_path / "nested" / "logs").exists()
