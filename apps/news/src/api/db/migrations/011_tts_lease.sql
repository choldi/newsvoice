-- 011_tts_lease.sql: priority engine lease for TTS synthesis (Phase 8.1).
--
-- The digest TTS step and the long-document render are served by the same
-- engines (the tts-api gateway on the host, Microsoft's edge-tts endpoint) from
-- *different containers*, so an in-process semaphore in either one coordinates
-- nothing. Both already share this SQLite file, which is opened in WAL mode
-- with a busy_timeout, so the lease lives here: no new dependency, no Redis.
--
--   tts_leases       at most one holder per scope (singleton row per scope).
--   tts_lease_waiters everyone currently waiting for a turn, FIFO. A waiter
--                     row with priority above the current holder's IS the
--                     event that makes a low-priority holder yield: the digest
--                     writes its row *before* asking for the lease, so the
--                     document render lets go before the digest starts, and
--                     the two never synthesize at the same time.
--
-- Both timestamps are ISO-8601 UTC strings, so they sort lexicographically and
-- are readable in a sqlite3 shell.

CREATE TABLE IF NOT EXISTS tts_leases (
    scope       TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 0,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tts_lease_waiters (
    owner        TEXT PRIMARY KEY,
    priority     INTEGER NOT NULL DEFAULT 0,
    requested_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tts_lease_waiters_priority
    ON tts_lease_waiters(priority, requested_at);
