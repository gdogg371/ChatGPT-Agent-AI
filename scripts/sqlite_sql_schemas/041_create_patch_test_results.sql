-- scripts/migrations/002_create_patch_test_results.sql
-- Version: 2.0
-- Created: 2025-07-19
-- Description: Schema for patch test result logs (syntax checks + script test outcomes)
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS patch_test_results (
    id INTEGER PRIMARY KEY,
    patch_id TEXT NOT NULL,
    file_path TEXT,
    syntax_ok INTEGER NOT NULL DEFAULT TRUE,
    script_tests_ok INTEGER NOT NULL DEFAULT TRUE,
    error_msg TEXT,
    script_log TEXT,
    tested_by TEXT NOT NULL DEFAULT 'system',
    tested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_patch_test_results_patch_id
    ON patch_test_results(patch_id);
CREATE INDEX IF NOT EXISTS idx_patch_test_results_status
    ON patch_test_results(syntax_ok, script_tests_ok);
-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- 
--
-- DROP TRIGGER IF EXISTS trg_update_patch_test_results_updated_at ON patch_test_results;
-- 
-- === Example Insert for Testing ===
INSERT INTO patch_test_results
    (patch_id, file_path, syntax_ok, script_tests_ok, error_msg, script_log)
VALUES
    ('test_patch_xyz', 'backend/utils/hello.py', TRUE, TRUE, NULL, 'All checks passed successfully.');
-- === End Migration ===
COMMIT;
COMMIT;
