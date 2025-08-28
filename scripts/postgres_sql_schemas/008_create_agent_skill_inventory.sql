-- scripts/migrations/008_create_agent_skill_inventory.sql
-- Version: 2.0
-- Created: 2024-07-06
-- Description: Master inventory of all skills known or available in the agent platform

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.agent_skill_inventory (
    id SERIAL PRIMARY KEY,
    skill_name VARCHAR(128) NOT NULL UNIQUE,
    category VARCHAR(64) NOT NULL DEFAULT 'general',
    description TEXT,
    is_core BOOLEAN NOT NULL DEFAULT FALSE,
    version VARCHAR(32) DEFAULT '1.0.0',
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Documentation and Traceability ===

COMMENT ON TABLE public.agent_skill_inventory IS
    'Centralized list of all skills available to agents in the platform. Used for skill discovery, documentation, and management.';

COMMENT ON COLUMN public.agent_skill_inventory.id IS
    'Primary key for skill inventory records.';
COMMENT ON COLUMN public.agent_skill_inventory.skill_name IS
    'Unique name or identifier for the skill.';
COMMENT ON COLUMN public.agent_skill_inventory.category IS
    'Logical grouping or category (e.g., perception, planning, I/O, web, utility).';
COMMENT ON COLUMN public.agent_skill_inventory.description IS
    'Optional human-readable description or notes for the skill.';
COMMENT ON COLUMN public.agent_skill_inventory.is_core IS
    'Boolean: TRUE if the skill is a core capability of the platform.';
COMMENT ON COLUMN public.agent_skill_inventory.version IS
    'Semantic version of the skill or its definition.';
COMMENT ON COLUMN public.agent_skill_inventory.metadata IS
    'Flexible JSONB: additional metadata, dependencies, external refs, etc.';
COMMENT ON COLUMN public.agent_skill_inventory.created_at IS
    'Row creation timestamp (for audit).';
COMMENT ON COLUMN public.agent_skill_inventory.updated_at IS
    'Row last update timestamp (for triggers/audit).';

-- === Indexes for Performance ===

CREATE INDEX IF NOT EXISTS idx_agent_skill_inventory_category
    ON public.agent_skill_inventory(category);

CREATE INDEX IF NOT EXISTS idx_agent_skill_inventory_is_core
    ON public.agent_skill_inventory(is_core);

-- === (Optional) Foreign Key Example (for future cross-skill mapping) ===
-- ALTER TABLE public.agent_skill_inventory
--     ADD CONSTRAINT fk_agent_skill_inventory_parent
--     FOREIGN KEY (parent_skill_id) REFERENCES public.agent_skill_inventory(id)
--     ON DELETE SET NULL;

-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- CREATE OR REPLACE FUNCTION update_agent_skill_inventory_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
--
-- DROP TRIGGER IF EXISTS trg_update_agent_skill_inventory_updated_at ON public.agent_skill_inventory;
-- CREATE TRIGGER trg_update_agent_skill_inventory_updated_at
--     BEFORE UPDATE ON public.agent_skill_inventory
--     FOR EACH ROW EXECUTE FUNCTION update_agent_skill_inventory_updated_at();

-- === Row-Level Security Policy Example (Optional) ===
-- ALTER TABLE public.agent_skill_inventory ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY agent_skill_inventory_all_access
--     ON public.agent_skill_inventory
--     USING (true);

-- === Example Insert for Testing ===

INSERT INTO public.agent_skill_inventory
    (skill_name, category, description, is_core, version)
VALUES
    ('file_write', 'I/O', 'Ability to write files to disk in allowed directories.', TRUE, '1.0.0');

-- === End Migration ===

COMMIT;

COMMIT;
