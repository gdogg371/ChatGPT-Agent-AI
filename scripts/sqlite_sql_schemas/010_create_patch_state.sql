-- scripts/migrations/010_create_patch_state.sql
-- Version: 2.0
-- Created: 2025-07-07
-- Description: Schema for patch lifecycle key-value state store (replaces patch_state.json)
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS patch_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    value_type TEXT NOT NULL DEFAULT 'jsonb',
    status TEXT NOT NULL DEFAULT 'active',
    notes TEXT DEFAULT NULL,
    created_by TEXT DEFAULT 'system',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
-- === Indexes (in addition to PK) ===
CREATE INDEX IF NOT EXISTS idx_patch_state_status
    ON patch_state(status);
CREATE INDEX IF NOT EXISTS idx_patch_state_type
    ON patch_state(value_type);
-- === Audit Trigger for updated_at (Optional) ===
-- 
--
-- DROP TRIGGER IF EXISTS trg_patch_state_timestamp ON patch_state;
-- 
-- === (Optional) RLS Support ===
-- 
-- 
-- === Example Insert for Testing ===
INSERT INTO patch_state (key, value, value_type, status, created_by)
VALUES
    ('lock', 'false', 'boolean', 'active', 'system');
-- === End Migration ===
COMMIT;
COMMIT;
