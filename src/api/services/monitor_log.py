"""Monitor event log store with daily rotation and retention cleanup.

Each monitor run appends one JSON line per check to
``data/logs/monitor_YYYY-MM-DD.jsonl`` (UTC day). Files older than the
configured retention window are removed on every append, so the log directory
stays bounded like the SQLite ``monitor_events`` table.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

from src.core.config import get_settings

logger = structlog.get_logger(__name__)


class MonitorLogStore:
    """Append JSONL monitor events with daily rotation and retention cleanup."""

    def __init__(self, log_dir: str | None = None, retention_days: int | None = None) -> None:
        """Initialize the store.

        Args:
            log_dir: Directory for the JSONL logs. Defaults to settings.monitor_log_dir.
            retention_days: Delete log files older than this many days.
                Defaults to settings.monitor_log_retention_days.
        """
        settings = get_settings()
        self._log_dir = Path(log_dir or settings.monitor_log_dir)
        self._retention_days = retention_days or settings.monitor_log_retention_days
        self._lock = asyncio.Lock()

    def _today_path(self) -> Path:
        """Return the path of today's (UTC) JSONL log file.

        Returns:
            Path under the log dir named monitor_YYYY-MM-DD.jsonl.
        """
        stamp = datetime.now(UTC).strftime("%Y-%m-%d")
        return self._log_dir / f"monitor_{stamp}.jsonl"

    async def _purge_expired(self) -> None:
        """Remove log files older than the retention window."""
        cutoff = datetime.now(UTC) - timedelta(days=self._retention_days)
        if not self._log_dir.exists():
            return
        for path in self._log_dir.glob("monitor_*.jsonl"):
            try:
                day = datetime.strptime(path.stem.split("_", 1)[1], "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                continue
            if day < cutoff:
                path.unlink(missing_ok=True)
                logger.info("monitor_log_purged", path=str(path))

    async def append(self, ts: str, check_name: str, status: str, detail: str | None) -> None:
        """Append a single check outcome as a JSON line to today's log.

        Args:
            ts: ISO timestamp of the event.
            check_name: Name of the check.
            status: Outcome ('ok', 'warning', 'error').
            detail: Optional human-readable detail.
        """
        async with self._lock:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            await self._purge_expired()
            record = {
                "ts": ts,
                "check": check_name,
                "status": status,
                "detail": detail,
            }
            with self._today_path().open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=True) + "\n")

    async def last_days(self, days: int = 3) -> list[dict]:
        """Return the JSONL records from the last ``days`` of log files.

        Args:
            days: How many daily files to read back, newest first.

        Returns:
            List of parsed JSON records across the daily files.
        """
        cutoff = datetime.now(UTC) - timedelta(days=days)
        results: list[dict] = []
        if not self._log_dir.exists():
            return results
        for path in sorted(self._log_dir.glob("monitor_*.jsonl"), reverse=True):
            try:
                day = datetime.strptime(path.stem.split("_", 1)[1], "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                continue
            if day < cutoff:
                break
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return results
