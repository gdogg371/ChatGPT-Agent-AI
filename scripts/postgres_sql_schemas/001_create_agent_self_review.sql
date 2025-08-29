-- scripts/migrations/001_create_agent_self_review.sql
-- Version: 2.0
-- Created: 2024-07-06
-- Description: Schema for agent self-review (periodic agent self-assessment/reflective logs)

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.agent_self_review (
    id SERIAL PRIMARY KEY,
    agent_id INTEGER NOT NULL,
    review_cycle INTEGER NOT NULL,
    review_timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    review_text TEXT NOT NULL,
    reviewer VARCHAR(128) NOT NULL DEFAULT 'self',
    score DOUBLE PRECISION CHECK (score >= 0.0 AND score <= 1.0),
    status VARCHAR(64) NOT NULL DEFAULT 'pending',
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.agent_self_review IS
    'Stores detailed self-review/self-assessment logs for agents, including timestamp, reviewer, cycle, score, and full metadata. Supports audit and reflection cycles.';

COMMENT ON COLUMN public.agent_self_review.id IS
    'Primary key (autoincrementing review record ID).';
COMMENT ON COLUMN public.agent_self_review.agent_id IS
    'References the unique agent (should FK to agents table).';
COMMENT ON COLUMN public.agent_self_review.review_cycle IS
    'Monotonically increasing review/reflection cycle number for each agent.';
COMMENT ON COLUMN public.agent_self_review.review_timestamp IS
    'When the review/reflection occurred.';
COMMENT ON COLUMN public.agent_self_review.review_text IS
    'Verbatim text, markdown, or JSON summary of the self-review/reflection.';
COMMENT ON COLUMN public.agent_self_review.reviewer IS
    'Identity of reviewer ("self" or agent label); used for agent-initiated or externally-audited reviews.';
COMMENT ON COLUMN public.agent_self_review.score IS
    'Optional numeric score/grade of the self-review (0.0 to 1.0).';
COMMENT ON COLUMN public.agent_self_review.status IS
    'Status: pending, complete, reviewed, flagged, or other.';
COMMENT ON COLUMN public.agent_self_review.metadata IS
    'Flexible JSONB: tags, external refs, agent version, and supplementary metadata.';
COMMENT ON COLUMN public.agent_self_review.created_at IS
    'Row creation timestamp (for audit, not mutable).';
COMMENT ON COLUMN public.agent_self_review.updated_at IS
    'Last-modified timestamp (should be updated on every update).';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_self_review_agent_cycle
    ON public.agent_self_review(agent_id, review_cycle);

CREATE INDEX IF NOT EXISTS idx_self_review_status
    ON public.agent_self_review(status);

-- === (Optional) Foreign Key Constraint - Uncomment and link if agents table exists ===
-- ALTER TABLE public.agent_self_review
--     ADD CONSTRAINT fk_agent_self_review_agent
--     FOREIGN KEY (agent_id) REFERENCES public.agents(id)
--     ON DELETE CASCADE;

-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- CREATE OR REPLACE FUNCTION update_agent_self_review_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
--
-- DROP TRIGGER IF EXISTS trg_update_self_review_updated_at ON public.agent_self_review;
-- CREATE TRIGGER trg_update_self_review_updated_at
--     BEFORE UPDATE ON public.agent_self_review
--     FOR EACH ROW EXECUTE FUNCTION update_agent_self_review_updated_at();

-- === Row Level Security Policy (Optional Example) ===
-- ALTER TABLE public.agent_self_review ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY agent_self_review_isolation
--     ON public.agent_self_review
--     USING (agent_id = current_setting('app.current_agent_id')::integer);

-- === Example Insert for Testing ===

INSERT INTO public.agent_self_review
    (agent_id, review_cycle, review_text, reviewer, score, status)
VALUES
    (1, 1, 'System initial self-review: all diagnostics passing, no errors detected.', 'self', 0.97, 'complete');

-- === End Migration ===

COMMIT;

COMMIT;
