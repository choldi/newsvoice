-- 010_verifications.sql
-- LLM output verification ledger (Phase 7.13). One row per verification of one
-- target, so a run keeps the full history of what was judged, by which method,
-- with which model and against which threshold. `target` discriminates what was
-- verified ('digest' today, 'narration_script'/'narration_audio' reserved for
-- Phase 7.14) and `method` which check produced the score.
--
-- `passed` is duplicated from the verdict so the GUI and the REST API can filter
-- on it with plain SQL instead of parsing the verdict string. `details` holds the
-- JSON breakdown (unsupported claims, ungrounded numbers, per-source scores).
CREATE TABLE IF NOT EXISTS verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    collection TEXT NOT NULL DEFAULT 'default',
    target TEXT NOT NULL,
    method TEXT NOT NULL,
    verdict TEXT NOT NULL DEFAULT 'error',
    score REAL,
    threshold REAL,
    passed INTEGER NOT NULL DEFAULT 0,
    details TEXT,
    model TEXT,
    duration_seconds REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_verifications_run_target ON verifications(run_id, target);
CREATE INDEX IF NOT EXISTS idx_verifications_collection ON verifications(collection);
