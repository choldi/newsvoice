-- 004_digest_runs.sql: Run history for the admin GUI (calendar/history/errors).
--
-- One row per digest pipeline execution, recorded by record_run_activity at the
-- end of every workflow run (success AND no-new-articles). Powers the Phase 6
-- web UI: calendar of events, history viewer, and error view.

CREATE TABLE IF NOT EXISTS digest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    collection TEXT NOT NULL DEFAULT 'default',
    status TEXT NOT NULL DEFAULT 'ok',
    article_count INTEGER NOT NULL DEFAULT 0,
    cluster_count INTEGER NOT NULL DEFAULT 0,
    digest_preview TEXT,
    audio_path TEXT,
    error TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_digest_runs_collection ON digest_runs(collection);
CREATE INDEX IF NOT EXISTS idx_digest_runs_status ON digest_runs(status);
CREATE INDEX IF NOT EXISTS idx_digest_runs_completed_at ON digest_runs(completed_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_digest_runs_workflow_id ON digest_runs(workflow_id);