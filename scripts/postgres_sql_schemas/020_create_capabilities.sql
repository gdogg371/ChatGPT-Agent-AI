-- scripts/migrations/001_create_capabilities.sql
-- Version: 2.0
-- Created: 2025-07-29
-- Description: Schema for persistent agent capabilities registry

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.capabilities (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL UNIQUE,
    description TEXT,
    confidence DOUBLE PRECISION CHECK (confidence >= 0.0 AND confidence <= 1.0) DEFAULT 1.0,
    source VARCHAR(64) NOT NULL DEFAULT 'manual',
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.capabilities IS
    'Canonical registry of agent capabilities, including confidence, source, metadata, and audit fields. Powers capability introspection, scope checks, and planning.';

COMMENT ON COLUMN public.capabilities.id IS
    'Primary key for the capability record.';
COMMENT ON COLUMN public.capabilities.name IS
    'Unique name of the capability (e.g., "ag1_patch_lifecycle").';
COMMENT ON COLUMN public.capabilities.description IS
    'Optional human-readable description of what this capability does.';
COMMENT ON COLUMN public.capabilities.confidence IS
    'System- or human-assigned trust/confidence in capability implementation (0.0 to 1.0).';
COMMENT ON COLUMN public.capabilities.source IS
    'Provenance of capability: "manual", "llm", "inferred", etc.';
COMMENT ON COLUMN public.capabilities.metadata IS
    'Flexible JSONB field for additional capability metadata, file links, AG mapping, etc.';
COMMENT ON COLUMN public.capabilities.created_at IS
    'Row creation timestamp (immutable).';
COMMENT ON COLUMN public.capabilities.updated_at IS
    'Timestamp of last update (used for audits).';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_capabilities_name
    ON public.capabilities(name);

CREATE INDEX IF NOT EXISTS idx_capabilities_source
    ON public.capabilities(source);

-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- CREATE OR REPLACE FUNCTION update_capabilities_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
--
-- DROP TRIGGER IF EXISTS trg_update_capabilities_updated_at ON public.capabilities;
-- CREATE TRIGGER trg_update_capabilities_updated_at
--     BEFORE UPDATE ON public.capabilities
--     FOR EACH ROW EXECUTE FUNCTION update_capabilities_updated_at();

-- === Row Level Security Policy (Optional Example) ===
-- ALTER TABLE public.capabilities ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY capabilities_agent_filter
--     ON public.capabilities
--     USING (current_setting('app.current_agent_id', true) IS NOT NULL);

-- === Example Insert for Testing ===

INSERT INTO public.capabilities
    (name, description, confidence, source)
VALUES
    ('ag1_patch_lifecycle', 'Tracks patch states, applies and rolls back patches.', 0.95, 'manual');

-- === End Migration ===

COMMIT;

COMMIT;
