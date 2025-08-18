-- scripts/migrations/009_create_agent_skill_prerequisites.sql
-- Version: 2.0
-- Created: 2024-07-06
-- Description: Defines prerequisite skill relationships between skills in the agent platform

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.agent_skill_prerequisites (
    id SERIAL PRIMARY KEY,
    skill_id INTEGER NOT NULL,
    prerequisite_skill_id INTEGER NOT NULL,
    required_level VARCHAR(32) DEFAULT 'basic',
    notes TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Documentation and Traceability ===

COMMENT ON TABLE public.agent_skill_prerequisites IS
    'Maps skill-to-skill prerequisite relationships for agent skills. Enables skill dependency resolution and planning.';

COMMENT ON COLUMN public.agent_skill_prerequisites.id IS
    'Primary key for skill prerequisite mapping.';
COMMENT ON COLUMN public.agent_skill_prerequisites.skill_id IS
    'ID of the skill that requires a prerequisite (FK to agent_skill_inventory).';
COMMENT ON COLUMN public.agent_skill_prerequisites.prerequisite_skill_id IS
    'ID of the prerequisite skill (FK to agent_skill_inventory).';
COMMENT ON COLUMN public.agent_skill_prerequisites.required_level IS
    'Minimum proficiency required in the prerequisite skill.';
COMMENT ON COLUMN public.agent_skill_prerequisites.notes IS
    'Optional notes about the prerequisite relationship.';
COMMENT ON COLUMN public.agent_skill_prerequisites.created_at IS
    'Row creation timestamp (for audit).';
COMMENT ON COLUMN public.agent_skill_prerequisites.updated_at IS
    'Row last update timestamp (for triggers/audit).';

-- === Indexes for Performance ===

CREATE INDEX IF NOT EXISTS idx_skill_prerequisites_skill_id
    ON public.agent_skill_prerequisites(skill_id);

CREATE INDEX IF NOT EXISTS idx_skill_prerequisites_prerequisite_skill_id
    ON public.agent_skill_prerequisites(prerequisite_skill_id);

-- === (Optional) Foreign Key Constraints (uncomment if agent_skill_inventory exists) ===
-- ALTER TABLE public.agent_skill_prerequisites
--     ADD CONSTRAINT fk_skill_prerequisites_skill
--     FOREIGN KEY (skill_id) REFERENCES public.agent_skill_inventory(id)
--     ON DELETE CASCADE;
--
-- ALTER TABLE public.agent_skill_prerequisites
--     ADD CONSTRAINT fk_skill_prerequisites_prereq
--     FOREIGN KEY (prerequisite_skill_id) REFERENCES public.agent_skill_inventory(id)
--     ON DELETE CASCADE;

-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- CREATE OR REPLACE FUNCTION update_agent_skill_prerequisites_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
--
-- DROP TRIGGER IF EXISTS trg_update_agent_skill_prerequisites_updated_at ON public.agent_skill_prerequisites;
-- CREATE TRIGGER trg_update_agent_skill_prerequisites_updated_at
--     BEFORE UPDATE ON public.agent_skill_prerequisites
--     FOR EACH ROW EXECUTE FUNCTION update_agent_skill_prerequisites_updated_at();

-- === Row-Level Security Policy Example (Optional) ===
-- ALTER TABLE public.agent_skill_prerequisites ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY agent_skill_prerequisites_all_access
--     ON public.agent_skill_prerequisites
--     USING (true);

-- === Example Insert for Testing ===

INSERT INTO public.agent_skill_prerequisites
    (skill_id, prerequisite_skill_id, required_level, notes)
VALUES
    (1, 2, 'advanced', 'Skill 1 requires Skill 2 at advanced level for activation.');

-- === End Migration ===

COMMIT;

COMMIT;
