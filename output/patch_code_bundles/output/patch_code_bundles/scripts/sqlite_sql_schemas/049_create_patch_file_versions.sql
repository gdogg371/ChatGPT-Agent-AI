-- scripts/migrations/002_create_patch_file_versions.sql
-- Version: 2.0
-- Created: 2025-07-27
-- Description: Schema for patch file version snapshots (versioned file content for diff, rollback, diagnostics)
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS patch_file_versions (
    id INTEGER PRIMARY KEY,
    patch_id INTEGER,
    path TEXT NOT NULL,
    version INTEGER NOT NULL,
    snapshot TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_patch_file_path_version
    ON patch_file_versions(path, version DESC);
CREATE INDEX IF NOT EXISTS idx_patch_file_patch_id
    ON patch_file_versions(patch_id);
-- === (Optional) Foreign Key Constraint - Uncomment if patches table exists ===
-- ALTER TABLE patch_file_versions
--     ADD CONSTRAINT fk_patch_file_versions_patch
--     FOREIGN KEY (patch_id) REFERENCES patch_requests(id)
--     ON DELETE CASCADE;
-- === Example Insert for Testing ===
INSERT INTO patch_file_versions
    (patch_id, path, version, snapshot)
VALUES
    (NULL, 'backend/core/memory/memory.py', 1, 'def get_all_memory_items():\n    return []');
-- === End Migration ===
COMMIT;
