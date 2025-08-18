-- scripts/migrations/013_create_patch_metadata.sql
-- Version: 2.0
-- Created: 2025-07-11
-- Description: Schema for storing patch metadata used in simulation, forecasting, and execution planning (AG48+)

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.patch_metadata (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    modifies TEXT,  -- Comma-separated list of modified domains (e.g. "trust,core")
    targets TEXT,   -- Comma-separated affected subsystems (e.g. "cli,core")
    reboot_required BOOLEAN NOT NULL DEFAULT FALSE,
    trust_level TEXT NOT NULL DEFAULT 'low',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.patch_metadata IS
    'Stores metadata for each patch (AG) to support forecasting, planning, and simulation workflows. Supports CLI, API, and automated patch evaluation.';

COMMENT ON COLUMN public.patch_metadata.id IS
    'Patch ID or AG label (e.g. "AG48"). Must be unique.';
COMMENT ON COLUMN public.patch_metadata.name IS
    'Human-readable name of the patch (used in CLI, UI, logs).';
COMMENT ON COLUMN public.patch_metadata.modifies IS
    'Comma-separated list of system components the patch modifies (e.g. "trust,diagnostics"). Used in risk scoring.';
COMMENT ON COLUMN public.patch_metadata.targets IS
    'Comma-separated list of logical targets (e.g. "cli,core"). Used for planner mapping and dependency inference.';
COMMENT ON COLUMN public.patch_metadata.reboot_required IS
    'Whether this patch requires a system restart or service reload.';
COMMENT ON COLUMN public.patch_metadata.trust_level IS
    'Trust rating: low, medium, high. Used in simulation scoring and HIL enforcement.';
COMMENT ON COLUMN public.patch_metadata.created_at IS
    'Patch metadata creation timestamp.';
COMMENT ON COLUMN public.patch_metadata.updated_at IS
    'Last updated timestamp (for metadata edits, patch replay, or rollback reasons).';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_patch_metadata_trust
    ON public.patch_metadata(trust_level);

CREATE INDEX IF NOT EXISTS idx_patch_metadata_targets
    ON public.patch_metadata(targets);

-- === Optional Trigger for updated_at field ===

-- CREATE OR REPLACE FUNCTION update_patch_metadata_timestamp()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
--
-- DROP TRIGGER IF EXISTS trg_patch_metadata_updated_at ON public.patch_metadata;
-- CREATE TRIGGER trg_patch_metadata_updated_at
--     BEFORE UPDATE ON public.patch_metadata
--     FOR EACH ROW EXECUTE FUNCTION update_patch_metadata_timestamp();

-- === Example Insert for Testing ===

INSERT INTO public.patch_metadata (id, name, modifies, targets, reboot_required, trust_level)
VALUES
    ('AG48', 'Patch Chain Simulator', 'forecast', 'planner,simulation', FALSE, 'low')
ON CONFLICT (id) DO NOTHING;

-- === End Migration ===

COMMIT;

COMMIT;
