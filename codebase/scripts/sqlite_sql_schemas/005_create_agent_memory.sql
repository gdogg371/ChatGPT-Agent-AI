-- scripts/migrations/005_create_agent_memory.sql
-- Version: 2.0
-- Created: 2024-07-06
-- Description: Agent long- and short-term memory store (episodic, semantic, and context memory records)
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS agent_memory (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL,
    memory_type TEXT NOT NULL DEFAULT 'episodic',
    content TEXT NOT NULL,
    context TEXT DEFAULT '{}',
    timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    relevance REAL CHECK (relevance >= 0.0 AND relevance <= 1.0),
    tags TEXT[],
    source TEXT DEFAULT 'internal',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Documentation and Traceability ===
-- === Indexes for Performance ===
CREATE INDEX IF NOT EXISTS idx_agent_memory_agent_id
    ON agent_memory(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_memory_type
    ON agent_memory(memory_type);
CREATE INDEX IF NOT EXISTS idx_agent_memory_tags
    ON agent_memory USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_agent_memory_status
    ON agent_memory(status);
-- === (Optional) Foreign Key Constraint (uncomment if agent_registry exists) ===
-- ALTER TABLE agent_memory
--     ADD CONSTRAINT fk_agent_memory_agent
--     FOREIGN KEY (agent_id) REFERENCES agent_registry(id)
--     ON DELETE CASCADE;
-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- 
--
-- DROP TRIGGER IF EXISTS trg_update_agent_memory_updated_at ON agent_memory;
-- 
-- === Row-Level Security Policy Example (Optional) ===
-- 
-- 
-- === Example Insert for Testing ===
INSERT INTO agent_memory
    (agent_id, memory_type, content, relevance, tags, source, status)
VALUES
    (1, 'episodic', 'Agent completed task: Initial system boot.', 0.99, ARRAY['boot','init','system'], 'internal', 'active');
-- === End Migration ===
COMMIT;
COMMIT;
