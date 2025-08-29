-- scripts/migrations/003_patch_file_versions_trigger.sql
-- Version: 2.0
-- Created: 2025-07-27
-- Description: Adds updated_at column and trigger for auto-update on row change

BEGIN;

-- === Add updated_at column (if not already present) ===

ALTER TABLE public.patch_file_versions
ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP;

-- === Create or Replace Trigger Function ===

CREATE OR REPLACE FUNCTION update_patch_file_versions_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- === Drop Existing Trigger (if any) ===

DROP TRIGGER IF EXISTS trg_update_patch_file_versions_updated_at ON public.patch_file_versions;

-- === Create Trigger for Auto-Updating updated_at ===

CREATE TRIGGER trg_update_patch_file_versions_updated_at
BEFORE UPDATE ON public.patch_file_versions
FOR EACH ROW
EXECUTE FUNCTION update_patch_file_versions_updated_at();

-- === Optional Commenting for Auditability ===

COMMENT ON COLUMN public.patch_file_versions.updated_at IS
    'Timestamp auto-updated via trigger on row modification.';

-- === End Migration ===

COMMIT;


COMMIT;
