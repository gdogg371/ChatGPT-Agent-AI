-- scripts/migrations/002_create_patch_state.sql
-- Version: 2.0
-- Created: 2024-07-12
-- Description: Schema for persistent patch state tracking (replaces patch_state.json)
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS patch_state (
    id INTEGER PRIMARY KEY,
    last_patch TEXT,
    active INTEGER NOT NULL DEFAULT FALSE,
    lock INTEGER NOT NULL DEFAULT FALSE,
    error TEXT,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE UNIQUE INDEX IF NOT EXISTS idx_patch_state_singleton
    ON patch_state((id)) WHERE id = 1;
CREATE INDEX IF NOT EXISTS idx_patch_state_active
    ON patch_state(active);
-- === Optional RLS Policy (Single-Agent Isolation) ===
-- 
-- 
-- === Audit Trigger for updated_at (Optional) ===
-- 
--
-- DROP TRIGGER IF EXISTS trg_update_patch_state_updated_at ON patch_state;
-- 
-- === Example Insert for Bootstrapping ===
INSERT INTO patch_state
    (last_patch, active, lock, error, metadata)
VALUES
    (NULL, FALSE, FALSE, NULL, '{"agent_id": "bootstrap", "env": "dev"}');
-- === End Migration ===
COMMIT;
COMMIT;
