-- scripts/migrations/009_create_agent_skill_prerequisites.sql
-- Version: 2.0
-- Created: 2024-07-06
-- Description: Defines prerequisite skill relationships between skills in the agent platform
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS agent_skill_prerequisites (
    id INTEGER PRIMARY KEY,
    skill_id INTEGER NOT NULL,
    prerequisite_skill_id INTEGER NOT NULL,
    required_level TEXT DEFAULT 'basic',
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Documentation and Traceability ===
-- === Indexes for Performance ===
CREATE INDEX IF NOT EXISTS idx_skill_prerequisites_skill_id
    ON agent_skill_prerequisites(skill_id);
CREATE INDEX IF NOT EXISTS idx_skill_prerequisites_prerequisite_skill_id
    ON agent_skill_prerequisites(prerequisite_skill_id);
-- === (Optional) Foreign Key Constraints (uncomment if agent_skill_inventory exists) ===
-- ALTER TABLE agent_skill_prerequisites
--     ADD CONSTRAINT fk_skill_prerequisites_skill
--     FOREIGN KEY (skill_id) REFERENCES agent_skill_inventory(id)
--     ON DELETE CASCADE;
--
-- ALTER TABLE agent_skill_prerequisites
--     ADD CONSTRAINT fk_skill_prerequisites_prereq
--     FOREIGN KEY (prerequisite_skill_id) REFERENCES agent_skill_inventory(id)
--     ON DELETE CASCADE;
-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- 
--
-- DROP TRIGGER IF EXISTS trg_update_agent_skill_prerequisites_updated_at ON agent_skill_prerequisites;
-- 
-- === Row-Level Security Policy Example (Optional) ===
-- 
-- 
-- === Example Insert for Testing ===
INSERT INTO agent_skill_prerequisites
    (skill_id, prerequisite_skill_id, required_level, notes)
VALUES
    (1, 2, 'advanced', 'Skill 1 requires Skill 2 at advanced level for activation.');
-- === End Migration ===
COMMIT;
COMMIT;
