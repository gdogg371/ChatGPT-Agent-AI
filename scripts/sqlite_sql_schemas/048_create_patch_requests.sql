-- scripts/migrations/002_create_patch_requests.sql
-- Version: 2.0
-- Created: 2025-07-23
-- Description: Schema for agent-submitted patch requests
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS patch_requests (
    id TEXT PRIMARY KEY,
    agent_id INTEGER NOT NULL,
    skill_name TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'submitted',
    source TEXT NOT NULL DEFAULT 'agent_loop',
    submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metadata TEXT DEFAULT '{}');
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_patch_requests_agent
    ON patch_requests(agent_id);
CREATE INDEX IF NOT EXISTS idx_patch_requests_status
    ON patch_requests(status);
CREATE INDEX IF NOT EXISTS idx_patch_requests_skill
    ON patch_requests(skill_name);
-- === (Optional) Foreign Key Constraint - Uncomment if agents table exists ===
-- ALTER TABLE patch_requests
--     ADD CONSTRAINT fk_patch_requests_agent
--     FOREIGN KEY (agent_id) REFERENCES agents(id)
--     ON DELETE CASCADE;
-- === Audit Trigger for updated_at (Optional, requires function) ===
-- 
--
-- DROP TRIGGER IF EXISTS trg_update_patch_requests_updated_at ON patch_requests;
-- 
-- === Row Level Security Policy (Optional) ===
-- 
-- 
-- === Example Insert for Testing ===
INSERT INTO patch_requests
    (id, agent_id, skill_name, plan_json, status)
VALUES
    ('patch_abc123', 1, 'memory.optimisation', '{"steps": ["add index", "refactor loop"]}', 'submitted');
-- === End Migration ===
COMMIT;
COMMIT;
