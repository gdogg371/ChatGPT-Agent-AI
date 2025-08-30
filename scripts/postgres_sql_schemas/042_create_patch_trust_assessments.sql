-- scripts/migrations/XXX_create_patch_trust_assessments.sql
-- Version: 1.0
-- Created: 2025-07-19
-- Description: Schema for storing trust evaluations of patches (AG39 trust assessments)

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.patch_trust_assessments (
    id SERIAL PRIMARY KEY,
    patch_id VARCHAR(128) NOT NULL,
    ag_reference TEXT[] NOT NULL,
    trust_score DOUBLE PRECISION CHECK (trust_score >= 0.0 AND trust_score <= 1.0),
    trusted BOOLEAN NOT NULL DEFAULT FALSE,
    reason TEXT NOT NULL,
    tested_by VARCHAR(128) NOT NULL DEFAULT 'self',
    tested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.patch_trust_assessments IS
    'Stores the result of trust assessments on patches, including AG match score, human-readable reasoning, and system trust decision. Used for AG39 logic and patch gatekeeping.';

COMMENT ON COLUMN public.patch_trust_assessments.id IS
    'Primary key (autoincrementing trust assessment ID).';
COMMENT ON COLUMN public.patch_trust_assessments.patch_id IS
    'ID of the evaluated patch (must match ID in patch_plan or patch_state).';
COMMENT ON COLUMN public.patch_trust_assessments.ag_reference IS
    'List of AG tags referenced by the patch.';
COMMENT ON COLUMN public.patch_trust_assessments.trust_score IS
    'Confidence score (0.0–1.0) of trust evaluation based on AG matching.';
COMMENT ON COLUMN public.patch_trust_assessments.trusted IS
    'Boolean indicator of whether patch is considered trustworthy.';
COMMENT ON COLUMN public.patch_trust_assessments.reason IS
    'Natural language explanation of trust result (e.g. "Score 0.82 based on AG match").';
COMMENT ON COLUMN public.patch_trust_assessments.tested_by IS
    'Label of evaluating system or user ("self" by default).';
COMMENT ON COLUMN public.patch_trust_assessments.tested_at IS
    'Timestamp of trust evaluation.';
COMMENT ON COLUMN public.patch_trust_assessments.metadata IS
    'Optional JSON metadata for traceability, linked patch file data, or trust assumptions.';
COMMENT ON COLUMN public.patch_trust_assessments.created_at IS
    'Insert timestamp (non-editable).';
COMMENT ON COLUMN public.patch_trust_assessments.updated_at IS
    'Last update timestamp (auto-set on update).';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_patch_trust_patch_id
    ON public.patch_trust_assessments(patch_id);

CREATE INDEX IF NOT EXISTS idx_patch_trust_score
    ON public.patch_trust_assessments(trust_score DESC);

-- === Audit Trigger for updated_at (Optional, requires a function) ===

-- CREATE OR REPLACE FUNCTION update_patch_trust_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;

-- DROP TRIGGER IF EXISTS trg_patch_trust_updated_at ON public.patch_trust_assessments;
-- CREATE TRIGGER trg_patch_trust_updated_at
--     BEFORE UPDATE ON public.patch_trust_assessments
--     FOR EACH ROW EXECUTE FUNCTION update_patch_trust_updated_at();

-- === Example Insert for Testing ===

INSERT INTO public.patch_trust_assessments
    (patch_id, ag_reference, trust_score, trusted, reason, tested_by)
VALUES
    ('patch_001', ARRAY['AG-3', 'AG-12'], 0.91, TRUE, 'Score 0.91 based on AG match with AG-3 and AG-12', 'self');

-- === End Migration ===

COMMIT;

COMMIT;
