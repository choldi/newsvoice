-- 009_modal_spend.sql
-- Modal GPU spend ledger (Phase 7.8). One row per Modal call, used by the
-- budget guard to enforce the daily and monthly caps before any .spawn().
CREATE TABLE IF NOT EXISTS modal_spend (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    collection TEXT NOT NULL,
    stage TEXT NOT NULL,
    backend TEXT NOT NULL,
    run_id TEXT,
    gpu_seconds REAL NOT NULL,
    cost_usd REAL NOT NULL,
    audio_seconds REAL,
    cache_hit INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_modal_spend_ts ON modal_spend(ts);
