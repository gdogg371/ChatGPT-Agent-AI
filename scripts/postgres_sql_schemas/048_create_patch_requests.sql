-- scripts/migrations/002_create_patch_requests.sql
-- Version: 2.0
-- Created: 2025-07-23
-- Description: Schema for agent-submitted patch requests

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.patch_requests (
    id TEXT PRIMARY KEY,
    agent_id INTEGER NOT NULL,
    skill_name TEXT NOT NULL,
    plan_json JSONB NOT NULL,
    status VARCHAR(64) NOT NULL DEFAULT 'submitted',
    source VARCHAR(128) NOT NULL DEFAULT 'agent_loop',
    submitted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metadata JSONB DEFAULT '{}'::jsonb
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.patch_requests IS
    'Tracks patch requests submitted by agents, including skill, plan content, and metadata for patch coordination and audit.';

COMMENT ON COLUMN public.patch_requests.id IS
    'Patch request ID (UUID, hash, or other unique key).';
COMMENT ON COLUMN public.patch_requests.agent_id IS
    'The agent that submitted the patch request (should FK to agents table).';
COMMENT ON COLUMN public.patch_requests.skill_name IS
    'Name of the capability or skill the patch affects.';
COMMENT ON COLUMN public.patch_requests.plan_json IS
    'Full JSON patch plan as submitted by the agent.';
COMMENT ON COLUMN public.patch_requests.status IS
    'Lifecycle status of the request: submitted, in_progress, complete, rejected, etc.';
COMMENT ON COLUMN public.patch_requests.source IS
    'Origin of the request (agent_loop, human_cli, etc.).';
COMMENT ON COLUMN public.patch_requests.submitted_at IS
    'Timestamp when patch request was submitted.';
COMMENT ON COLUMN public.patch_requests.updated_at IS
    'Timestamp when the record was last updated.';
COMMENT ON COLUMN public.patch_requests.metadata IS
    'Flexible JSONB field for trust score, AG tags, confidence levels, or user comments.';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_patch_requests_agent
    ON public.patch_requests(agent_id);

CREATE INDEX IF NOT EXISTS idx_patch_requests_status
    ON public.patch_requests(status);

CREATE INDEX IF NOT EXISTS idx_patch_requests_skill
    ON public.patch_requests(skill_name);

-- === (Optional) Foreign Key Constraint - Uncomment if agents table exists ===
-- ALTER TABLE public.patch_requests
--     ADD CONSTRAINT fk_patch_requests_agent
--     FOREIGN KEY (agent_id) REFERENCES public.agents(id)
--     ON DELETE CASCADE;

-- === Audit Trigger for updated_at (Optional, requires function) ===
-- CREATE OR REPLACE FUNCTION update_patch_requests_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
--
-- DROP TRIGGER IF EXISTS trg_update_patch_requests_updated_at ON public.patch_requests;
-- CREATE TRIGGER trg_update_patch_requests_updated_at
--     BEFORE UPDATE ON public.patch_requests
--     FOR EACH ROW EXECUTE FUNCTION update_patch_requests_updated_at();

-- === Row Level Security Policy (Optional) ===
-- ALTER TABLE public.patch_requests ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY patch_requests_isolation
--     ON public.patch_requests
--     USING (agent_id = current_setting('app.current_agent_id')::integer);

-- === Example Insert for Testing ===

INSERT INTO public.patch_requests
    (id, agent_id, skill_name, plan_json, status)
VALUES
    ('patch_abc123', 1, 'memory.optimisation', '{"steps": ["add index", "refactor loop"]}', 'submitted');

-- === End Migration ===

COMMIT;

COMMIT;
