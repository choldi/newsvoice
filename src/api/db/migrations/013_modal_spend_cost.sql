-- 013_modal_spend_cost.sql
-- Measure the real cost of Modal calls (Phase 7.17). One row per call already
-- exists (009); these columns let it carry the wall-clock measurement and the
-- billing-summary reconciliation on top of the pre-call estimate.
--
--   wall_seconds      client-observed seconds around the call
--   cost_source       'estimate' | 'runtime_rates' | 'billing_delta'
--   gpu_type          short GPU name ('l4', 'h100'), NULL for CPU-only calls
--   billed_delta_usd  change in the workspace billing summary across the call
--
-- Existing rows keep their values: the ALTERs only add nullable columns, and
-- cost_source has a default so old 'estimate' rows stay well-defined.
ALTER TABLE modal_spend ADD COLUMN wall_seconds REAL;
ALTER TABLE modal_spend ADD COLUMN cost_source TEXT NOT NULL DEFAULT 'estimate';
ALTER TABLE modal_spend ADD COLUMN gpu_type TEXT;
ALTER TABLE modal_spend ADD COLUMN billed_delta_usd REAL;
