-- scripts/migrations/017_lock_state.sql
-- Version: 2.0
-- Created: 2024-07-10
-- Description: Extended lock_state schema with TTL, expiry, concurrency policy, and RLS hooks
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS lock_state (
    lock_name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT FALSE,
    holder TEXT,
    since TEXT,
    ttl_seconds INTEGER DEFAULT 900 CHECK (ttl_seconds > 0),
    expires_at TEXT GENERATED ALWAYS AS (
        CASE
            WHEN since IS NOT NULL AND ttl_seconds IS NOT NULL THEN
                since + (ttl_seconds || ' seconds')
            ELSE NULL
        END
    ) STORED,
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_lock_state_active
    ON lock_state(value, expires_at);
CREATE INDEX IF NOT EXISTS idx_lock_state_holder
    ON lock_state(holder);
-- === Audit Trigger for updated_at (Optional) ===
-- 
-- DROP TRIGGER IF EXISTS trg_lock_state_updated_at ON lock_state;
-- 
-- === Row Level Security (Optional) ===
-- 
-- 
-- === Bootstrap Lock Definitions ===
INSERT INTO lock_state (lock_name, value, holder, ttl_seconds)
VALUES
    ('patch_lock', FALSE, NULL, 600),
    ('planner_lock', FALSE, NULL, 600),
    ('goal_lock_agent_1', FALSE, NULL, 900)
ON CONFLICT (lock_name) DO NOTHING;
-- === End Migration ===
COMMIT;
COMMIT;
