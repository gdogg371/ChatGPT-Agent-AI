-- scripts/migrations/NNN_create_patch_action_log.sql
-- Version: 2.0
-- Created: 2025-07-20
-- Description: Schema for logging patch action execution results and metadata
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS patch_action_log (
    id INTEGER PRIMARY KEY,
    patch_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    target TEXT,
    payload_preview TEXT,
    status TEXT NOT NULL CHECK (status IN ('success', 'failure', 'unsupported', 'exception')),
    message TEXT,
    execution_order INTEGER DEFAULT 0,
    timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metadata TEXT DEFAULT '{}');
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_patch_action_log_patch_id
    ON patch_action_log(patch_id);
CREATE INDEX IF NOT EXISTS idx_patch_action_log_status
    ON patch_action_log(status);
CREATE INDEX IF NOT EXISTS idx_patch_action_log_timestamp
    ON patch_action_log(timestamp);
-- === Example Insert for Testing ===
INSERT INTO patch_action_log
    (patch_id, action_type, target, payload_preview, status, message, execution_order)
VALUES
    ('test_patch_001', 'write', '/tmp/example.txt', 'Hello World', 'success', 'Wrote to example.txt', 1);
-- === End Migration ===
COMMIT;
