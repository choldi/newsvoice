-- 014_tts_lease_note.sql: why the engine is held (merge phase P8).
--
-- The design window (voice synthesis on the GPU) joins the priority lease as
-- another holder, and a bare owner id does not distinguish "design window is
-- open" from a digest run id when the operator asks who owns the GPU. `note`
-- carries that reason: the design-window CLI writes 'design-window', the
-- digest and renders keep writing '' (they are identified by their owner).
--
-- Default '' keeps every pre-existing row valid, and SQLite's ALTER TABLE
-- cannot add a second column in one statement, so one statement is all this
-- migration is.

ALTER TABLE tts_leases ADD COLUMN note TEXT NOT NULL DEFAULT '';
