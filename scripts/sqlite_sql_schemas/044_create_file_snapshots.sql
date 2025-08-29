-- scripts/migrations/002_create_file_snapshots.sql
-- Version: 2.0
-- Created: 2025-07-20
-- Description: Schema for pre/post file state snapshots used in patch integrity verification
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS file_snapshots (
    id INTEGER PRIMARY KEY,
    snapshot_id UUID NOT NULL,
    agent_id INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    snapshot_type TEXT NOT NULL CHECK (snapshot_type IN ('pre', 'post')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_snapshot_id
    ON file_snapshots(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_file_path
    ON file_snapshots(file_path);
CREATE INDEX IF NOT EXISTS idx_snapshot_type_path
    ON file_snapshots(snapshot_type, file_path);
-- === Foreign Key Constraint (Optional) ===
-- Uncomment if `agents` table exists
-- ALTER TABLE file_snapshots
--     ADD CONSTRAINT fk_file_snapshots_agent
--     FOREIGN KEY (agent_id) REFERENCES agents(id)
--     ON DELETE CASCADE;
-- === Audit Trigger for updated_at (Optional) ===
-- 
-- DROP TRIGGER IF EXISTS trg_update_file_snapshots_updated_at ON file_snapshots;
-- 
-- === Row Level Security Policy (Optional) ===
-- 
-- 
-- === Example Insert for Testing ===
INSERT INTO file_snapshots (
    snapshot_id, agent_id, file_path, file_hash, snapshot_type
)
VALUES (
    gen_random_uuid(), 1, '/app/main.py', 'abc123def456', 'pre'
);
-- === End Migration ===
COMMIT;
COMMIT;
