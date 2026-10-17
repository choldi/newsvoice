-- 007_monitor.sql: system monitor activity log.
--
-- Records the outcome of every check run by SystemMonitorWorkflow so the admin
-- can audit what happened even when no alert was sent. Entries older than
-- MONITOR_RETENTION_DAYS are purged on each monitor run.

CREATE TABLE IF NOT EXISTS monitor_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    check_name TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_monitor_events_check_ts ON monitor_events (check_name, ts);
CREATE INDEX IF NOT EXISTS idx_monitor_events_ts ON monitor_events (ts);