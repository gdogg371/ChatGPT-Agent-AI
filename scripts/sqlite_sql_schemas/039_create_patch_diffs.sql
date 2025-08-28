-- scripts/migrations/002_create_patch_diffs.sql
-- Version: 2.0
-- Created: 2025-07-19
-- Description: Schema for storing per-file patch diffs with audit and traceability
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS patch_diffs (
    id INTEGER PRIMARY KEY,
    patch_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    diff_text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_patch_diffs_patch_file
    ON patch_diffs(patch_id, filename);
-- === Audit Trigger for updated_at (Optional, requires function) ===
-- 
--
-- DROP TRIGGER IF EXISTS trg_update_patch_diffs_updated_at ON patch_diffs;
-- 
-- === Example Insert for Testing ===
INSERT INTO patch_diffs (patch_id, filename, diff_text)
VALUES
    ('patch_20250719_001', 'backend/core/example.py', '--- old\n+++ new\n@@ -1,1 +1,1 @@\n-print("hello")\n+print("world")');
-- === End Migration ===
COMMIT;
COMMIT;
