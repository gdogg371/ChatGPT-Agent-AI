-- scripts/migrations/007_create_agent_skills.sql
-- Version: 2.0
-- Created: 2024-07-06
-- Description: Table for agent skill definitions and capabilities inventory

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.agent_skills (
    id SERIAL PRIMARY KEY,
    agent_id INTEGER NOT NULL,
    skill_name VARCHAR(128) NOT NULL,
    skill_level VARCHAR(32) NOT NULL DEFAULT 'basic',
    acquired_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    description TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Documentation and Traceability ===

COMMENT ON TABLE public.agent_skills IS
    'Inventory of skills acquired or enabled for each agent. Tracks skill name, level, state, and additional metadata.';

COMMENT ON COLUMN public.agent_skills.id IS
    'Primary key for agent skills.';
COMMENT ON COLUMN public.agent_skills.agent_id IS
    'Agent to whom this skill record belongs (FK to agent_registry).';
COMMENT ON COLUMN public.agent_skills.skill_name IS
    'Name or identifier of the skill (e.g., "file_write", "web_scrape").';
COMMENT ON COLUMN public.agent_skills.skill_level IS
    'Skill proficiency or tier: basic, advanced, expert, etc.';
COMMENT ON COLUMN public.agent_skills.acquired_at IS
    'Timestamp when the skill was acquired/enabled.';
COMMENT ON COLUMN public.agent_skills.is_active IS
    'Whether the skill is currently active/enabled.';
COMMENT ON COLUMN public.agent_skills.description IS
    'Optional description or notes about the skill.';
COMMENT ON COLUMN public.agent_skills.metadata IS
    'Structured skill metadata as JSONB: version, dependencies, performance, etc.';
COMMENT ON COLUMN public.agent_skills.created_at IS
    'When this row was created (audit).';
COMMENT ON COLUMN public.agent_skills.updated_at IS
    'When this row was last modified (for triggers/audit).';

-- === Indexes for Performance ===

CREATE INDEX IF NOT EXISTS idx_agent_skills_agent_id
    ON public.agent_skills(agent_id);

CREATE INDEX IF NOT EXISTS idx_agent_skills_skill_name
    ON public.agent_skills(skill_name);

CREATE INDEX IF NOT EXISTS idx_agent_skills_is_active
    ON public.agent_skills(is_active);

-- === (Optional) Foreign Key Constraint (uncomment if agent_registry exists) ===
-- ALTER TABLE public.agent_skills
--     ADD CONSTRAINT fk_agent_skills_agent
--     FOREIGN KEY (agent_id) REFERENCES public.agent_registry(id)
--     ON DELETE CASCADE;

-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- CREATE OR REPLACE FUNCTION update_agent_skills_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
--
-- DROP TRIGGER IF EXISTS trg_update_agent_skills_updated_at ON public.agent_skills;
-- CREATE TRIGGER trg_update_agent_skills_updated_at
--     BEFORE UPDATE ON public.agent_skills
--     FOR EACH ROW EXECUTE FUNCTION update_agent_skills_updated_at();

-- === Row-Level Security Policy Example (Optional) ===
-- ALTER TABLE public.agent_skills ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY agent_skills_agent_access
--     ON public.agent_skills
--     USING (agent_id = current_setting('app.current_agent_id')::integer);

-- === Example Insert for Testing ===

INSERT INTO public.agent_skills
    (agent_id, skill_name, skill_level, is_active, description)
VALUES
    (1, 'web_scrape', 'advanced', TRUE, 'Can extract data from web pages and APIs.');

-- === End Migration ===

COMMIT;

COMMIT;
