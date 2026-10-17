"""Unit tests for the monitor reporter (alerts, summary, run notifications)."""

from datetime import UTC, datetime
from pathlib import Path

from src.api.db.database import MonitorRepository, SyncStateRepository
from src.api.models.monitoring import CheckResult, MonitorReport
from src.api.services.monitor_log import MonitorLogStore
from src.api.services.monitoring.reporter import (
    LAST_RUN_TS_KEY,
    MonitorReporter,
    format_alert_message,
)


def _check(name: str, status: str, detail: str | None = None) -> CheckResult:
    fields: dict[str, str] = {"check": name, "status": status}
    if detail is not None:
        fields["detail"] = detail
    return CheckResult(**fields)


def _report(*checks: CheckResult) -> MonitorReport:
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    return MonitorReport(ts=ts, checks=list(checks), alert=list(checks))


async def test_decide_alerts_first_run_alerts_on_errors() -> None:
    reporter = MonitorReporter()
    report = _report(_check("edge_tts", "error", "down"))

    alerts = await reporter.decide_alerts(report)
    assert [a.check for a in alerts] == ["edge_tts"]


async def test_decide_alerts_no_repeat_for_persistent_warning() -> None:
    reporter = MonitorReporter()
    report = _report(_check("services", "warning", "api down"))

    first = await reporter.decide_alerts(report)
    await reporter.record_last_statuses(report)
    second = await reporter.decide_alerts(report)

    assert len(first) == 1
    assert second == []


async def test_decide_alerts_recovery_silent_then_reup_alert() -> None:
    reporter = MonitorReporter()
    await reporter.record_last_statuses(_report(_check("docker", "ok")))

    alerts = await reporter.decide_alerts(_report(_check("docker", "warning")))
    assert [a.check for a in alerts] == ["docker"]


async def test_persist_writes_db_and_log(tmp_path: Path) -> None:
    reporter = MonitorReporter(
        monitor_repo=MonitorRepository(),
        log_store=MonitorLogStore(log_dir=str(tmp_path), retention_days=3),
    )
    report = _report(_check("a", "ok"), _check("b", "error", "boom"))

    await reporter.persist(report)

    assert await reporter.monitor_repo.count() == 2
    records = await reporter.log_store.last_days(days=1)
    assert [r["check"] for r in records] == ["a", "b"]
    assert records[1]["detail"] == "boom"


async def test_summary_day_roundtrip() -> None:
    reporter = MonitorReporter()
    assert await reporter.last_summary_day() is None

    await reporter.set_summary_day("2026-09-21")
    assert await reporter.last_summary_day() == "2026-09-21"


class _FakeNewRuns:
    def __init__(self, runs: list[dict]) -> None:
        self.runs = runs

    async def list_runs(self, limit: int = 20) -> list[dict]:
        return self.runs


async def test_new_completed_runs_reports_only_newer(tmp_path: Path) -> None:
    runs = _FakeNewRuns(
        [
            {
                "collection": "tech",
                "status": "ok",
                "article_count": 5,
                "audio_path": "x.mp3",
                "completed_at": "2026-09-21T09:00:00+00:00",
            },
            {
                "collection": "health",
                "status": "ok",
                "article_count": 3,
                "audio_path": None,
                "completed_at": "2026-09-21T09:30:00+00:00",
            },
        ]
    )
    reporter = MonitorReporter(runs_repo=runs)  # type: ignore[arg-type]

    first = await reporter.new_completed_runs()
    second = await reporter.new_completed_runs()

    assert len(first) == 2
    assert first[0] == "tech: ok (5 articles, audio=y)"
    assert second == []
    assert await SyncStateRepository().get(LAST_RUN_TS_KEY) == "2026-09-21T09:30:00+00:00"


async def test_new_completed_runs_missing_completed_uses_started() -> None:
    runs = _FakeNewRuns(
        [
            {
                "collection": "tech",
                "status": "no_new_articles",
                "article_count": 0,
                "audio_path": None,
                "started_at": "2026-09-21T08:00:00+00:00",
            }
        ]
    )
    reporter = MonitorReporter(runs_repo=runs)  # type: ignore[arg-type]

    lines = await reporter.new_completed_runs()
    assert lines == ["tech: no_new_articles (0 articles, audio=n)"]


def test_format_alert_message_alert_only() -> None:
    report = _report(
        _check("ok-check", "ok"),
        _check("services", "warning", "api down"),
    )
    message = format_alert_message(report)
    assert "News-engine alert" in message
    assert "ok-check" in message  # alert list mirrors the report checks here
    assert "api down" in message


def test_format_alert_message_summary_includes_all() -> None:
    report = _report(_check("edge_tts", "ok"))
    message = format_alert_message(report, is_summary=True)
    assert "Daily news-engine health" in message
    assert "edge_tts" in message
