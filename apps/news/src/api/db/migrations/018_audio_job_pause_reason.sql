-- 018_audio_job_pause_reason.sql: distinguish why a render is paused.
--
-- A PAUSED job can mean two very different things: the operator pressed pause,
-- or a long audiobook/document render handed the engine to a scheduled digest
-- that outranks it. The GUI must tell them apart (one resumes on its own, the
-- other waits for the operator), and the worker already knows which one caused
-- the transition. Store it next to the status so every API/UI surface can show
-- the right label.
--
-- Values: 'operator' (manual pause), 'lease' (yielded to a higher-priority
-- waiter), NULL when the job is not paused.

ALTER TABLE audio_jobs ADD COLUMN pause_reason TEXT;
