-- scripts/migrations/002_create_patch_risk_evaluations.sql
-- Version: 2.0
-- Created: 2025-07-19
-- Description: Schema for patch risk evaluations (risk score, HIL, reboot, auto-apply safety)

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.patch_risk_evaluations (
    id SERIAL PRIMARY KEY,
    patch_id VARCHAR(128) NOT NULL,
    evaluated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    risk_score DOUBLE PRECISION CHECK (risk_score >= 0.0 AND risk_score <= 1.0),
    human_required BOOLEAN NOT NULL DEFAULT FALSE,
    reboot_required BOOLEAN NOT NULL DEFAULT FALSE,
    is_safe_to_autoapply BOOLEAN NOT NULL DEFAULT FALSE,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.patch_risk_evaluations IS
    'Stores risk analysis metadata for individual patches, including HIL requirement, reboot status, risk score, and automation safety.';

COMMENT ON COLUMN public.patch_risk_evaluations.id IS
    'Primary key (autoincrementing row ID).';
COMMENT ON COLUMN public.patch_risk_evaluations.patch_id IS
    'Identifier of the patch being evaluated (should match name in patch metadata).';
COMMENT ON COLUMN public.patch_risk_evaluations.evaluated_at IS
    'Timestamp of the risk evaluation.';
COMMENT ON COLUMN public.patch_risk_evaluations.risk_score IS
    'Computed risk score between 0.0 (safe) and 1.0 (unsafe).';
COMMENT ON COLUMN public.patch_risk_evaluations.human_required IS
    'Whether human approval is required before applying the patch.';
COMMENT ON COLUMN public.patch_risk_evaluations.reboot_required IS
    'Whether patch requires system reboot post-application.';
COMMENT ON COLUMN public.patch_risk_evaluations.is_safe_to_autoapply IS
    'Boolean flag indicating whether the patch is safe to auto-apply.';
COMMENT ON COLUMN public.patch_risk_evaluations.metadata IS
    'Optional structured metadata, including reasons, sources, and flags.';
COMMENT ON COLUMN public.patch_risk_evaluations.created_at IS
    'When the row was first created.';
COMMENT ON COLUMN public.patch_risk_evaluations.updated_at IS
    'Last update timestamp (auto-managed via trigger).';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_risk_eval_patch_time
    ON public.patch_risk_evaluations(patch_id, evaluated_at DESC);

CREATE INDEX IF NOT EXISTS idx_risk_eval_autoapply
    ON public.patch_risk_evaluations(is_safe_to_autoapply);

-- === Audit Trigger for updated_at (Optional, requires a function) ===

-- CREATE OR REPLACE FUNCTION update_patch_risk_eval_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
--
-- DROP TRIGGER IF EXISTS trg_update_patch_risk_eval_updated_at ON public.patch_risk_evaluations;
-- CREATE TRIGGER trg_update_patch_risk_eval_updated_at
--     BEFORE UPDATE ON public.patch_risk_evaluations
--     FOR EACH ROW EXECUTE FUNCTION update_patch_risk_eval_updated_at();

-- === Row Level Security Policy (Optional Example) ===
-- ALTER TABLE public.patch_risk_evaluations ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY patch_risk_eval_visibility
--     ON public.patch_risk_evaluations
--     USING (patch_id LIKE current_setting('app.current_patch_prefix') || '%');

-- === Example Insert for Testing ===

INSERT INTO public.patch_risk_evaluations
    (patch_id, risk_score, human_required, reboot_required, is_safe_to_autoapply)
VALUES
    ('hotfix-secure-logrotate', 0.18, FALSE, FALSE, TRUE);

-- === End Migration ===

COMMIT;

COMMIT;
