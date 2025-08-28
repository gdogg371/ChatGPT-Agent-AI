-- scripts/migrations/015_lock_state.sql
-- Version: 2.0
-- Created: 2024-07-10
-- Description: Schema for DB-backed system-wide locks (e.g. patch_lock, agent_lock, planner_lock)
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS lock_state (
    lock_name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT FALSE,
    holder TEXT,
    since TEXT,
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_lock_state_value
    ON lock_state(value);
CREATE INDEX IF NOT EXISTS idx_lock_state_holder
    ON lock_state(holder);
-- === Audit Trigger for updated_at ===
-- 
-- DROP TRIGGER IF EXISTS trg_update_lock_state_updated_at ON lock_state;
-- 
-- === Row Level Security (Optional Example) ===
-- 
-- 
-- === Initial Lock Definitions ===
INSERT INTO lock_state (lock_name, value, holder)
VALUES
    ('patch_lock', FALSE, NULL),
    ('planner_lock', FALSE, NULL),
    ('goal_lock_agent_1', FALSE, NULL)
ON CONFLICT (lock_name) DO NOTHING;
-- === End Migration ===
COMMIT;
COMMIT;
