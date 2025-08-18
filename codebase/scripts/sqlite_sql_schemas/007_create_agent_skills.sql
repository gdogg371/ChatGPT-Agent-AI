-- scripts/migrations/007_create_agent_skills.sql
-- Version: 2.0
-- Created: 2024-07-06
-- Description: Table for agent skill definitions and capabilities inventory
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS agent_skills (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL,
    skill_name TEXT NOT NULL,
    skill_level TEXT NOT NULL DEFAULT 'basic',
    acquired_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_active INTEGER NOT NULL DEFAULT TRUE,
    description TEXT,
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Documentation and Traceability ===
-- === Indexes for Performance ===
CREATE INDEX IF NOT EXISTS idx_agent_skills_agent_id
    ON agent_skills(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_skills_skill_name
    ON agent_skills(skill_name);
CREATE INDEX IF NOT EXISTS idx_agent_skills_is_active
    ON agent_skills(is_active);
-- === (Optional) Foreign Key Constraint (uncomment if agent_registry exists) ===
-- ALTER TABLE agent_skills
--     ADD CONSTRAINT fk_agent_skills_agent
--     FOREIGN KEY (agent_id) REFERENCES agent_registry(id)
--     ON DELETE CASCADE;
-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- 
--
-- DROP TRIGGER IF EXISTS trg_update_agent_skills_updated_at ON agent_skills;
-- 
-- === Row-Level Security Policy Example (Optional) ===
-- 
-- 
-- === Example Insert for Testing ===
INSERT INTO agent_skills
    (agent_id, skill_name, skill_level, is_active, description)
VALUES
    (1, 'web_scrape', 'advanced', TRUE, 'Can extract data from web pages and APIs.');
-- === End Migration ===
COMMIT;
COMMIT;
