"""Aggregate, persist, and decide the alerts of a monitor report.

The :class:`MonitorReporter` lives in a service (not the workflow) so the
transition bookkeeping (SQLite ``kv_state``) and the JSONL/Database event
recording stay out of the deterministic workflow; the Temporal activity calls
into it directly.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.api.db.database import MonitorRepository, RunsRepository, SyncStateRepository
from src.api.models.monitoring import CheckResult, MonitorReport
from src.api.services.monitor_log import MonitorLogStore

LAST_STATUS_PREFIX = "monitor:last_status:"
SUMMARY_DAY_KEY = "monitor:last_summary_day"
LAST_RUN_TS_KEY = "monitor:last_run_ts"


class MonitorReporter:
    """Persist monitor events and compute which checks need an alert."""

    def __init__(
        self,
        monitor_repo: MonitorRepository | None = None,
        log_store: MonitorLogStore | None = None,
        state_repo: SyncStateRepository | None = None,
        runs_repo: RunsRepository | None = None,
    ) -> None:
        """Initialize with injectable dependencies (override in tests).

        Args:
            monitor_repo: SQLite monitor_events repository.
            log_store: JSONL log store.
            state_repo: KV state repository.
            runs_repo: Digest runs repository (for new-run notifications).
        """
        self.monitor_repo = monitor_repo or MonitorRepository()
        self.log_store = log_store or MonitorLogStore()
        self.state_repo = state_repo or SyncStateRepository()
        self.runs_repo = runs_repo or RunsRepository()

    async def persist(self, report: MonitorReport) -> None:
        """Write every check outcome to SQLite and the JSONL log.

        Also purges old events beyond the configured retention window.

        Args:
            report: The monitor report to persist.
        """
        for check in report.checks:
            await self.monitor_repo.record_event(check.check, check.status, check.detail)
            await self.log_store.append(report.ts, check.check, check.status, check.detail)

    async def decide_alerts(self, report: MonitorReport) -> list[CheckResult]:
        """Return checks whose status changed from a healthy one (or first run).

        Stops a persistent warning from re-alerting on every tick: a check
        alerts when it now is 'error'/'warning' while the last recorded status
        was 'ok', 'skipped', or missing.

        Args:
            report: The freshly collected report.

        Returns:
            The subset of checks to alert on.
        """
        alerts: list[CheckResult] = []
        for check in report.checks:
            if check.status not in ("error", "warning"):
                continue
            last = await self.state_repo.get(f"{LAST_STATUS_PREFIX}{check.check}")
            if last not in ("error", "warning"):
                alerts.append(check)
        return alerts

    async def record_last_statuses(self, report: MonitorReport) -> None:
        """Store the latest status per check so transitions are detectable.

        Args:
            report: The monitor report just collected.
        """
        for check in report.checks:
            await self.state_repo.set(f"{LAST_STATUS_PREFIX}{check.check}", check.status)

    @staticmethod
    def summary_day() -> str:
        """Return the current UTC day key (YYYY-MM-DD).

        Returns:
            UTC day string used to detect when to send the daily summary.
        """
        return datetime.now(UTC).strftime("%Y-%m-%d")

    async def last_summary_day(self) -> str | None:
        """Return the day the last daily summary was sent.

        Returns:
            The stored UTC day string, or None.
        """
        return await self.state_repo.get(SUMMARY_DAY_KEY)

    async def set_summary_day(self, day: str) -> None:
        """Remember the day a daily summary was sent.

        Args:
            day: UTC day string (YYYY-MM-DD).
        """
        await self.state_repo.set(SUMMARY_DAY_KEY, day)

    async def new_completed_runs(self) -> list[str]:
        """Return a summary line per digest run completed since the last tick.

        Args:
            None.

        Returns:
            List of human-readable run summaries for newly completed runs.
        """
        runs = await self.runs_repo.list_runs(limit=20)
        last_seen = await self.state_repo.get(LAST_RUN_TS_KEY)
        new_lines: list[str] = []
        newest_ts = last_seen
        for run in runs:
            ts = run.get("completed_at") or run.get("started_at") or ""
            if newest_ts is None or (ts and ts > newest_ts):
                newest_ts = ts or newest_ts
            if last_seen is None or (ts and ts > last_seen):
                new_lines.append(
                    f"{run.get('collection')}: {run.get('status')} "
                    f"({run.get('article_count', 0)} articles, "
                    f"audio={'y' if run.get('audio_path') else 'n'})"
                )
        if newest_ts:
            await self.state_repo.set(LAST_RUN_TS_KEY, newest_ts)
        return new_lines


def format_alert_message(report: MonitorReport, is_summary: bool = False) -> str:
    """Render the alert or daily-summary text for delivery.

    Args:
        report: The monitor report.
        is_summary: When True, includes every check (daily summary) instead of
            only the alert subset.

    Returns:
        A plain-text message suitable for Telegram/email/WhatsApp.
    """
    header = "Daily news-engine health" if is_summary else "News-engine alert"
    lines = [f"\U0001f4ca {header} ({report.ts[:16]}Z)", ""]
    checks = report.checks if is_summary else report.alert
    for check in checks:
        icon = {
            "ok": "\u2705",
            "warning": "\u26a0\ufe0f",
            "error": "\u274c",
            "info": "\U0001f680",
        }.get(check.status, "\u27a1\ufe0f")
        detail = check.detail or check.status
        lines.append(f"{icon} {check.check}: {detail}")
    return "\n".join(lines)
