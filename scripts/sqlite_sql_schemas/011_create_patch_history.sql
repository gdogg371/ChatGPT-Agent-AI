-- scripts/migrations/011_create_patch_history.sql
-- Version: 2.0
-- Created: 2025-07-07
-- Description: Schema for patch event audit log (applied/rollback/failure trace)
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS patch_history (
    id INTEGER PRIMARY KEY,
    patch_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('applied', 'rolled_back', 'failed', 'recovered', 'planned')
    ),
    agent_id INTEGER,
    actor TEXT DEFAULT 'system',
    metadata TEXT DEFAULT '{}',
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_patch_history_patch_id
    ON patch_history(patch_id);
CREATE INDEX IF NOT EXISTS idx_patch_history_status
    ON patch_history(status);
CREATE INDEX IF NOT EXISTS idx_patch_history_agent
    ON patch_history(agent_id);
-- === RLS Policy (Optional) ===
-- 
-- 
-- === Example Insert for Testing ===
INSERT INTO patch_history
    (patch_id, status, agent_id, actor, metadata, notes)
VALUES
    ('2025_07_initial_patch', 'applied', 1, 'system', '{"duration": "5s"}', 'Initial bootstrapping patch applied successfully.');
-- === End Migration ===
COMMIT;
