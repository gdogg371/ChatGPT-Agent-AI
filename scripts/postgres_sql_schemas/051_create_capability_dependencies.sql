-- scripts/migrations/002_create_capability_dependencies.sql
-- Version: 2.0
-- Created: 2025-07-29
-- Description: Capability dependency graph (edges between capabilities)

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.capability_dependencies (
    id SERIAL PRIMARY KEY,
    capability_name VARCHAR(128) NOT NULL,
    depends_on VARCHAR(128) NOT NULL,
    relation_type VARCHAR(64) NOT NULL DEFAULT 'hard',
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.capability_dependencies IS
    'Directed edges in the capability dependency graph. Indicates that capability A depends on capability B.';

COMMENT ON COLUMN public.capability_dependencies.id IS
    'Primary key for the dependency edge.';
COMMENT ON COLUMN public.capability_dependencies.capability_name IS
    'Name of the capability that has the dependency (the source node).';
COMMENT ON COLUMN public.capability_dependencies.depends_on IS
    'Name of the capability being depended on (the target node).';
COMMENT ON COLUMN public.capability_dependencies.relation_type IS
    'Dependency type: "hard", "soft", "implied", etc.';
COMMENT ON COLUMN public.capability_dependencies.metadata IS
    'Optional JSONB metadata (notes, conditions, scoring).';
COMMENT ON COLUMN public.capability_dependencies.created_at IS
    'Timestamp when the edge was created.';
COMMENT ON COLUMN public.capability_dependencies.updated_at IS
    'Timestamp when the edge was last updated.';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_dep_capability_name
    ON public.capability_dependencies(capability_name);

CREATE INDEX IF NOT EXISTS idx_dep_depends_on
    ON public.capability_dependencies(depends_on);

-- === Foreign Key Hints (Optional, if capabilities table FK needed) ===
-- ALTER TABLE public.capability_dependencies
--     ADD CONSTRAINT fk_dep_capability
--     FOREIGN KEY (capability_name) REFERENCES public.capabilities(name)
--     ON DELETE CASCADE;
--
-- ALTER TABLE public.capability_dependencies
--     ADD CONSTRAINT fk_dep_depends_on
--     FOREIGN KEY (depends_on) REFERENCES public.capabilities(name)
--     ON DELETE CASCADE;

-- === Audit Trigger for updated_at (Optional) ===
-- CREATE OR REPLACE FUNCTION update_capability_dependencies_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
--
-- DROP TRIGGER IF EXISTS trg_update_capability_dependencies_updated_at ON public.capability_dependencies;
-- CREATE TRIGGER trg_update_capability_dependencies_updated_at
--     BEFORE UPDATE ON public.capability_dependencies
--     FOR EACH ROW EXECUTE FUNCTION update_capability_dependencies_updated_at();

-- === Example Insert for Testing ===

INSERT INTO public.capability_dependencies
    (capability_name, depends_on, relation_type)
VALUES
    ('ag9_patch_planner', 'ag1_patch_lifecycle', 'hard');

-- === End Migration ===

COMMIT;

COMMIT;
