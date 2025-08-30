-- scripts/migrations/002_create_patch_plan_files.sql
-- Version: 2.0
-- Created: 2025-07-22
-- Description: Normalized table for individual file actions in each patch plan.
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS patch_plan_files (
    id INTEGER PRIMARY KEY,
    patch_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'modify',
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_patch_plan_file_patch
    ON patch_plan_files(patch_id);
CREATE INDEX IF NOT EXISTS idx_patch_plan_file_path
    ON patch_plan_files(file_path);
-- === Foreign Key Constraint (assumes patch_plan table exists) ===
ALTER TABLE patch_plan_files
    ADD CONSTRAINT fk_patch_plan_files_plan
    FOREIGN KEY (patch_id) REFERENCES patch_plan(id)
    ON DELETE CASCADE;
-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- 
--
-- DROP TRIGGER IF EXISTS trg_update_patch_plan_files_updated_at ON patch_plan_files;
-- 
-- === Row Level Security Policy (Optional Example) ===
-- 
-- 
-- === Example Insert for Testing ===
INSERT INTO patch_plan_files
    (patch_id, file_path, action, metadata)
VALUES
    ('AG12_mem_fix', 'backend/core/memory.py', 'modify', '{"reason":"fix memory retention edge case"}');
-- === End Migration ===
COMMIT;
COMMIT;
