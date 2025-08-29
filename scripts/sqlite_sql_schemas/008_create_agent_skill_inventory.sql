-- scripts/migrations/008_create_agent_skill_inventory.sql
-- Version: 2.0
-- Created: 2024-07-06
-- Description: Master inventory of all skills known or available in the agent platform
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS agent_skill_inventory (
    id INTEGER PRIMARY KEY,
    skill_name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL DEFAULT 'general',
    description TEXT,
    is_core INTEGER NOT NULL DEFAULT FALSE,
    version TEXT DEFAULT '1.0.0',
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Documentation and Traceability ===
-- === Indexes for Performance ===
CREATE INDEX IF NOT EXISTS idx_agent_skill_inventory_category
    ON agent_skill_inventory(category);
CREATE INDEX IF NOT EXISTS idx_agent_skill_inventory_is_core
    ON agent_skill_inventory(is_core);
-- === (Optional) Foreign Key Example (for future cross-skill mapping) ===
-- ALTER TABLE agent_skill_inventory
--     ADD CONSTRAINT fk_agent_skill_inventory_parent
--     FOREIGN KEY (parent_skill_id) REFERENCES agent_skill_inventory(id)
--     ON DELETE SET NULL;
-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- 
--
-- DROP TRIGGER IF EXISTS trg_update_agent_skill_inventory_updated_at ON agent_skill_inventory;
-- 
-- === Row-Level Security Policy Example (Optional) ===
-- 
-- 
-- === Example Insert for Testing ===
INSERT INTO agent_skill_inventory
    (skill_name, category, description, is_core, version)
VALUES
    ('file_write', 'I/O', 'Ability to write files to disk in allowed directories.', TRUE, '1.0.0');
-- === End Migration ===
COMMIT;
COMMIT;
