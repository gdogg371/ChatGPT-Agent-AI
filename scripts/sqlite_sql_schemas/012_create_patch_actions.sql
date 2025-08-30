-- scripts/migrations/012_create_patch_actions.sql
-- Version: 2.0
-- Created: 2025-07-07
-- Description: Schema for audit-log of granular patch actions (e.g., per step inside a patch plan)
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS patch_actions (
    id INTEGER PRIMARY KEY,
    patch_id TEXT NOT NULL,
    action_index INTEGER NOT NULL CHECK (action_index >= 0),
    action_type TEXT NOT NULL CHECK (
        action_type IN ('copy', 'replace', 'delete', 'exec', 'noop')
    ),
    target TEXT NOT NULL,
    source TEXT,
    status TEXT DEFAULT 'pending',
    result TEXT,
    metadata TEXT DEFAULT '{}',
    agent_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_patch_actions_patch_id
    ON patch_actions(patch_id);
CREATE INDEX IF NOT EXISTS idx_patch_actions_status
    ON patch_actions(status);
-- === Optional FK (deferred constraint to allow log-first) ===
-- ALTER TABLE patch_actions
-- ADD CONSTRAINT fk_patch_action_history
-- FOREIGN KEY (patch_id) REFERENCES patch_history(patch_id)
-- ON DELETE CASCADE;
-- === Optional RLS ===
-- 
-- 
-- === Example Insert for Testing ===
INSERT INTO patch_actions
    (patch_id, action_index, action_type, target, source, status, result, agent_id)
VALUES
    ('2025_07_initial_patch', 0, 'copy', '/etc/config.yaml', '/tmp/config.yaml', 'success', 'Overwritten OK', 1);
-- === End Migration ===
COMMIT;
