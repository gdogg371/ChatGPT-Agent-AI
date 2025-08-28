-- scripts/migrations/005_create_agent_memory.sql
-- Version: 2.0
-- Created: 2024-07-06
-- Description: Agent long- and short-term memory store (episodic, semantic, and context memory records)

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.agent_memory (
    id SERIAL PRIMARY KEY,
    agent_id INTEGER NOT NULL,
    memory_type VARCHAR(64) NOT NULL DEFAULT 'episodic',
    content TEXT NOT NULL,
    context JSONB DEFAULT '{}'::jsonb,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    relevance DOUBLE PRECISION CHECK (relevance >= 0.0 AND relevance <= 1.0),
    tags TEXT[],
    source VARCHAR(128) DEFAULT 'internal',
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Documentation and Traceability ===

COMMENT ON TABLE public.agent_memory IS
    'Stores agent memory items: events, context, facts, and episodic or semantic data for recall, reasoning, or reporting.';

COMMENT ON COLUMN public.agent_memory.id IS
    'Primary key for memory records.';
COMMENT ON COLUMN public.agent_memory.agent_id IS
    'Agent this memory belongs to (FK to agent_registry).';
COMMENT ON COLUMN public.agent_memory.memory_type IS
    'Category/type: episodic, semantic, context, summary, etc.';
COMMENT ON COLUMN public.agent_memory.content IS
    'Content of the memory (plain text, summary, or serialized info).';
COMMENT ON COLUMN public.agent_memory.context IS
    'Structured context as JSONB: entities, references, event details.';
COMMENT ON COLUMN public.agent_memory.timestamp IS
    'Timestamp when the memory event/fact occurred (can differ from creation).';
COMMENT ON COLUMN public.agent_memory.relevance IS
    'Optional relevance/confidence score (0.0–1.0) for ranking and recall.';
COMMENT ON COLUMN public.agent_memory.tags IS
    'Optional: array of tag strings for filtering or grouping.';
COMMENT ON COLUMN public.agent_memory.source IS
    'Memory source: internal, user, subsystem, etc.';
COMMENT ON COLUMN public.agent_memory.status IS
    'Current status: active, archived, expired, flagged.';
COMMENT ON COLUMN public.agent_memory.created_at IS
    'When this row was created (audit).';
COMMENT ON COLUMN public.agent_memory.updated_at IS
    'When this row was last modified (for audit triggers).';

-- === Indexes for Performance ===

CREATE INDEX IF NOT EXISTS idx_agent_memory_agent_id
    ON public.agent_memory(agent_id);

CREATE INDEX IF NOT EXISTS idx_agent_memory_type
    ON public.agent_memory(memory_type);

CREATE INDEX IF NOT EXISTS idx_agent_memory_tags
    ON public.agent_memory USING GIN (tags);

CREATE INDEX IF NOT EXISTS idx_agent_memory_status
    ON public.agent_memory(status);

-- === (Optional) Foreign Key Constraint (uncomment if agent_registry exists) ===
-- ALTER TABLE public.agent_memory
--     ADD CONSTRAINT fk_agent_memory_agent
--     FOREIGN KEY (agent_id) REFERENCES public.agent_registry(id)
--     ON DELETE CASCADE;

-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- CREATE OR REPLACE FUNCTION update_agent_memory_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
--
-- DROP TRIGGER IF EXISTS trg_update_agent_memory_updated_at ON public.agent_memory;
-- CREATE TRIGGER trg_update_agent_memory_updated_at
--     BEFORE UPDATE ON public.agent_memory
--     FOR EACH ROW EXECUTE FUNCTION update_agent_memory_updated_at();

-- === Row-Level Security Policy Example (Optional) ===
-- ALTER TABLE public.agent_memory ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY agent_memory_agent_access
--     ON public.agent_memory
--     USING (agent_id = current_setting('app.current_agent_id')::integer);

-- === Example Insert for Testing ===

INSERT INTO public.agent_memory
    (agent_id, memory_type, content, relevance, tags, source, status)
VALUES
    (1, 'episodic', 'Agent completed task: Initial system boot.', 0.99, ARRAY['boot','init','system'], 'internal', 'active');

-- === End Migration ===

COMMIT;

COMMIT;
