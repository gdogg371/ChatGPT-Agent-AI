-- scripts/migrations/002_create_agent_registry.sql
-- Version: 2.0
-- Created: 2024-07-06
-- Description: Schema for registered agent definitions, identity, and configuration
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS agent_registry (
    id INTEGER PRIMARY KEY,
    agent_name TEXT NOT NULL UNIQUE,
    public_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    role TEXT NOT NULL DEFAULT 'autonomous_agent',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    config TEXT DEFAULT '{}',
    description TEXT,
    owner TEXT,
    last_seen TEXT,
    is_trusted INTEGER NOT NULL DEFAULT FALSE
);
-- === Comments for Documentation and Traceability ===
-- === Indexes for Performance ===
CREATE INDEX IF NOT EXISTS idx_agent_registry_status
    ON agent_registry(status);
CREATE INDEX IF NOT EXISTS idx_agent_registry_owner
    ON agent_registry(owner);
-- === (Optional) Foreign Key Examples (uncomment and adapt as needed) ===
-- ALTER TABLE agent_registry
--     ADD CONSTRAINT fk_agent_registry_owner
--     FOREIGN KEY (owner) REFERENCES users(username)
--     ON DELETE SET NULL;
-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- 
--
-- DROP TRIGGER IF EXISTS trg_update_agent_registry_updated_at ON agent_registry;
-- 
-- === Row-Level Security Policy Example (Optional) ===
-- 
-- 
-- === Example Insert for Testing ===
INSERT INTO agent_registry
    (agent_name, public_key, status, role, config, description, owner, is_trusted)
VALUES
    ('agent_core_1', '-----BEGIN PUBLIC KEY-----MIIBIjANBgkqh...IDAQAB-----END PUBLIC KEY-----', 'active', 'autonomous_agent', '{"learning":true,"max_goals":5}', 'Main core agent, responsible for orchestration', 'admin', TRUE);
-- === End Migration ===
COMMIT;
COMMIT;
COMMIT;
