-- 017_audiobook_controls.sql: operator controls for long audiobook renders.
--
-- A book render can run for hours, so the operator needs to pause it (freeing
-- the engine for a digest) and resume it later, and to generate only a slice
-- of the chapters for a quick check. Those decisions live in the database
-- rather than the job metadata sidecar: the API writes them while the worker
-- polls, and SQLite's single-writer model makes the flag atomic across the two
-- containers (the sidecar is a read-modify-write JSON file).
--
-- ``partial_path`` is the last partial M4B built from the finished chapters;
-- it is advisory (the file is cacheable and rebuilt on demand).

ALTER TABLE audio_jobs ADD COLUMN paused INTEGER NOT NULL DEFAULT 0;
ALTER TABLE audio_jobs ADD COLUMN chapter_start INTEGER;
ALTER TABLE audio_jobs ADD COLUMN chapter_end INTEGER;
ALTER TABLE audio_jobs ADD COLUMN partial_path TEXT;
