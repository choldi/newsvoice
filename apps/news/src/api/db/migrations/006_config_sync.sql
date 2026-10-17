-- 006_config_sync.sql: last-registered signature of collections.toml.
--
-- The collections-config monitor compares the current sha256 of collections.toml
-- against the value stored here; when they differ, it re-registers the Temporal
-- schedules so the running schedules always match the file on disk.

CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);