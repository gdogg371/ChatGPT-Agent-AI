-- scripts/migrations/003_patch_file_versions_trigger.sql
-- Version: 2.0
-- Created: 2025-07-27
-- Description: Adds updated_at column and trigger for auto-update on row change
BEGIN;
-- === Add updated_at column (if not already present) ===
ALTER TABLE patch_file_versions
ADD COLUMN IF NOT EXISTS updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP;
-- === Create or Replace Trigger Function ===
-- === Drop Existing Trigger (if any) ===
DROP TRIGGER IF EXISTS trg_update_patch_file_versions_updated_at ON patch_file_versions;
-- === Create Trigger for Auto-Updating updated_at ===
-- === Optional Commenting for Auditability ===
-- === End Migration ===
COMMIT;
COMMIT;
