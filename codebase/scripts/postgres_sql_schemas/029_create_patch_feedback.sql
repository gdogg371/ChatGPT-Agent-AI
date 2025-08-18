-- scripts/migrations/002_create_patch_feedback.sql
-- Version: 2.0
-- Created: 2025-07-19
-- Description: Schema for patch feedback and LLM reflective feedback loop results

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.patch_feedback (
    id SERIAL PRIMARY KEY,
    patch_id VARCHAR(128) NOT NULL,
    feedback_text TEXT NOT NULL,
    feedback_type VARCHAR(64) NOT NULL DEFAULT 'llm',
    source VARCHAR(128) NOT NULL DEFAULT 'agent',
    model_used VARCHAR(64) DEFAULT NULL,
    prompt_used TEXT DEFAULT NULL,
    status VARCHAR(64) NOT NULL DEFAULT 'complete',
    score DOUBLE PRECISION CHECK (score >= 0.0 AND score <= 1.0),
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.patch_feedback IS
    'Stores patch-specific feedback results returned by LLM or internal review logic. Includes raw feedback, model metadata, prompt, status, and audit fields.';

COMMENT ON COLUMN public.patch_feedback.id IS
    'Primary key (autoincremented feedback entry ID).';
COMMENT ON COLUMN public.patch_feedback.patch_id IS
    'Patch identifier this feedback is associated with.';
COMMENT ON COLUMN public.patch_feedback.feedback_text IS
    'Verbatim text response from LLM or human/agent feedback system.';
COMMENT ON COLUMN public.patch_feedback.feedback_type IS
    'Type of feedback: llm, self, human_review, test_harness.';
COMMENT ON COLUMN public.patch_feedback.source IS
    'Source of feedback (agent ID, system, username, or "agent"/"user").';
COMMENT ON COLUMN public.patch_feedback.model_used IS
    'Model identifier used to generate this feedback (e.g. gpt-4, llama3).';
COMMENT ON COLUMN public.patch_feedback.prompt_used IS
    'Optional prompt input that triggered the feedback request.';
COMMENT ON COLUMN public.patch_feedback.status IS
    'Status of the feedback: pending, complete, flagged, ignored.';
COMMENT ON COLUMN public.patch_feedback.score IS
    'Optional confidence/accuracy/review score on a 0.0–1.0 scale.';
COMMENT ON COLUMN public.patch_feedback.metadata IS
    'Flexible JSONB for model config, tags, system version, or run context.';
COMMENT ON COLUMN public.patch_feedback.created_at IS
    'Row creation timestamp.';
COMMENT ON COLUMN public.patch_feedback.updated_at IS
    'Row last-updated timestamp.';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_patch_feedback_patch_id
    ON public.patch_feedback(patch_id);

CREATE INDEX IF NOT EXISTS idx_patch_feedback_status
    ON public.patch_feedback(status);

CREATE INDEX IF NOT EXISTS idx_patch_feedback_type
    ON public.patch_feedback(feedback_type);

-- === Optional: Trigger to auto-update updated_at ===
-- CREATE OR REPLACE FUNCTION update_patch_feedback_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
--
-- DROP TRIGGER IF EXISTS trg_update_patch_feedback_updated_at ON public.patch_feedback;
-- CREATE TRIGGER trg_update_patch_feedback_updated_at
--     BEFORE UPDATE ON public.patch_feedback
--     FOR EACH ROW EXECUTE FUNCTION update_patch_feedback_updated_at();

-- === Example Insert for Testing ===

INSERT INTO public.patch_feedback
    (patch_id, feedback_text, feedback_type, source, model_used, score)
VALUES
    ('patch_001', 'Patch applied successfully. All test cases passed.', 'llm', 'agent', 'gpt-4', 0.98);

-- === End Migration ===

COMMIT;

COMMIT;
