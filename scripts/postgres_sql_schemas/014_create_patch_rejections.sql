-- scripts/migrations/014_patch_rejections.sql
-- Version: 2.0
-- Created: 2024-07-10
-- Description: Schema for logging patch rejections with reason, source, and timestamp

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.patch_rejections (
    id SERIAL PRIMARY KEY,
    patch_id INTEGER NOT NULL,
    reason_code VARCHAR(64) NOT NULL,
    details TEXT,
    rejected_by VARCHAR(64) NOT NULL DEFAULT 'agent',
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.patch_rejections IS
    'Stores rejection metadata for patches, including rejection reason codes, source of rejection, and contextual details. Supports diagnostics, audit, and AG45a planning safeguards.';

COMMENT ON COLUMN public.patch_rejections.id IS
    'Primary key for each rejection record.';
COMMENT ON COLUMN public.patch_rejections.patch_id IS
    'Foreign key linking to patch_history.';
COMMENT ON COLUMN public.patch_rejections.reason_code IS
    'Short reason code (e.g. CHECKSUM_MISMATCH, HUMAN_REJECTED, SYNTAX_FAIL).';
COMMENT ON COLUMN public.patch_rejections.details IS
    'Detailed explanation, stack trace, or message explaining the rejection.';
COMMENT ON COLUMN public.patch_rejections.rejected_by IS
    'Entity that rejected the patch (e.g. agent, human, CLI, validator).';
COMMENT ON COLUMN public.patch_rejections.timestamp IS
    'When the patch was rejected.';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_patch_rejections_patch
    ON public.patch_rejections(patch_id);

-- === (Optional) Foreign Key Constraint - Uncomment if patch_history exists ===
-- ALTER TABLE public.patch_rejections
--     ADD CONSTRAINT fk_patch_rejections_patch
--     FOREIGN KEY (patch_id) REFERENCES public.patch_history(id)
--     ON DELETE CASCADE;

-- === Example Insert for Testing ===

INSERT INTO public.patch_rejections
    (patch_id, reason_code, details, rejected_by)
VALUES
    (1, 'HUMAN_REJECTED', 'User declined patch via CLI approval process.', 'human');

-- === End Migration ===

COMMIT;
