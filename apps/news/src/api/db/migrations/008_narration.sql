-- 008_narration.sql: multi-speaker narration pipeline (Phase 7, foundations).
--
-- Three changes:
--  1. `audio_jobs` gains a `kind` discriminator. The old UNIQUE(run_id) index
--     made a second audio pipeline in the same run overwrite the first one, so
--     the digest TTS job ('tts') and the narration job ('narration') can now
--     coexist. Existing rows default to 'tts' and keep their behaviour.
--  2. `digest_runs` gains `narration_path`. `audio_path` stays reserved for the
--     single-voice digest TTS output.
--  3. `narration_scripts` stores the authored script (LLM, editor or API) that
--     the narration renderer turns into audio, one row per save.

ALTER TABLE audio_jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'tts';

DROP INDEX IF EXISTS idx_audio_jobs_run_id;
CREATE UNIQUE INDEX IF NOT EXISTS idx_audio_jobs_run_id_kind ON audio_jobs(run_id, kind);

ALTER TABLE digest_runs ADD COLUMN narration_path TEXT;

CREATE TABLE IF NOT EXISTS narration_scripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    collection TEXT NOT NULL DEFAULT 'default',
    source TEXT NOT NULL DEFAULT 'llm',
    language TEXT NOT NULL DEFAULT 'es',
    title TEXT NOT NULL DEFAULT '',
    script_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_narration_scripts_run_id ON narration_scripts(run_id);
CREATE INDEX IF NOT EXISTS idx_narration_scripts_collection ON narration_scripts(collection);
