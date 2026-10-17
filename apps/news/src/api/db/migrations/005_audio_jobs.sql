-- 005_audio_jobs.sql: TTS generation status for the admin GUI.
--
-- One row per TTS job (keyed by the parent run's workflow_id). The digest
-- workflow keeps synthesizing audio inside the run, but clients can check the
-- exact status/error/elapsed instead of guessing from the run row alone.

CREATE TABLE IF NOT EXISTS audio_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    collection TEXT NOT NULL DEFAULT 'default',
    status TEXT NOT NULL DEFAULT 'STARTING',
    error TEXT,
    audio_path TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    elapsed_seconds REAL
);

CREATE INDEX IF NOT EXISTS idx_audio_jobs_collection ON audio_jobs(collection);
CREATE UNIQUE INDEX IF NOT EXISTS idx_audio_jobs_run_id ON audio_jobs(run_id);