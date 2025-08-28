-- scripts/migrations/006_create_patch_failures.sql
-- Version: 2.0
-- Created: 2024-07-06
-- Description: Table for recording patch/apply failures, diagnostics, and recovery attempts
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS patch_failures (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER,
    patch_id TEXT NOT NULL,
    failure_time TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    error_message TEXT NOT NULL,
    error_details TEXT DEFAULT '{}',
    severity TEXT NOT NULL DEFAULT 'error',
    recovery_status TEXT NOT NULL DEFAULT 'pending',
    recovery_action TEXT,
    resolved_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Documentation and Traceability ===
-- === Indexes for Performance ===
CREATE INDEX IF NOT EXISTS idx_patch_failures_agent_id
    ON patch_failures(agent_id);
CREATE INDEX IF NOT EXISTS idx_patch_failures_patch_id
    ON patch_failures(patch_id);
CREATE INDEX IF NOT EXISTS idx_patch_failures_severity
    ON patch_failures(severity);
CREATE INDEX IF NOT EXISTS idx_patch_failures_recovery_status
    ON patch_failures(recovery_status);
-- === (Optional) Foreign Key Constraint (uncomment if agent_registry exists) ===
-- ALTER TABLE patch_failures
--     ADD CONSTRAINT fk_patch_failures_agent
--     FOREIGN KEY (agent_id) REFERENCES agent_registry(id)
--     ON DELETE SET NULL;
-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- 
--
-- DROP TRIGGER IF EXISTS trg_update_patch_failures_updated_at ON patch_failures;
-- 
-- === Row-Level Security Policy Example (Optional) ===
-- 
-- 
-- === Example Insert for Testing ===
INSERT INTO patch_failures
    (agent_id, patch_id, error_message, error_details, severity, recovery_status)
VALUES
    (1, 'patch_20240706_001', 'Failed to apply schema migration: missing column', '{"trace":"KeyError: column not found"}', 'error', 'pending');
-- === End Migration ===
COMMIT;
COMMIT;
