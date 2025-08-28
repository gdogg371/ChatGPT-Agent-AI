-- scripts/migrations/006_create_patch_failures.sql
-- Version: 2.0
-- Created: 2024-07-06
-- Description: Table for recording patch/apply failures, diagnostics, and recovery attempts

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.patch_failures (
    id SERIAL PRIMARY KEY,
    agent_id INTEGER,
    patch_id VARCHAR(128) NOT NULL,
    failure_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    error_message TEXT NOT NULL,
    error_details JSONB DEFAULT '{}'::jsonb,
    severity VARCHAR(32) NOT NULL DEFAULT 'error',
    recovery_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    recovery_action TEXT,
    resolved_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Documentation and Traceability ===

COMMENT ON TABLE public.patch_failures IS
    'Logs all failed patch/apply events for agents, including diagnostics, recovery attempts, and resolution status. Enables audit and automated recovery.';

COMMENT ON COLUMN public.patch_failures.id IS
    'Primary key for patch failure records.';
COMMENT ON COLUMN public.patch_failures.agent_id IS
    'Agent that experienced the patch failure (FK to agent_registry).';
COMMENT ON COLUMN public.patch_failures.patch_id IS
    'Identifier of the patch/upgrade/job (UUID, hash, or name).';
COMMENT ON COLUMN public.patch_failures.failure_time IS
    'Timestamp of failure detection or report.';
COMMENT ON COLUMN public.patch_failures.error_message IS
    'Short description or summary of the failure.';
COMMENT ON COLUMN public.patch_failures.error_details IS
    'Structured JSONB with stack trace, environment, logs, etc.';
COMMENT ON COLUMN public.patch_failures.severity IS
    'Severity level: error, warning, critical, etc.';
COMMENT ON COLUMN public.patch_failures.recovery_status IS
    'Current status: pending, in_progress, resolved, failed, skipped.';
COMMENT ON COLUMN public.patch_failures.recovery_action IS
    'Optional notes or steps for manual/automated recovery.';
COMMENT ON COLUMN public.patch_failures.resolved_at IS
    'If resolved, timestamp when recovery completed.';
COMMENT ON COLUMN public.patch_failures.created_at IS
    'Row creation timestamp.';
COMMENT ON COLUMN public.patch_failures.updated_at IS
    'Row last update timestamp (for triggers/audit).';

-- === Indexes for Performance ===

CREATE INDEX IF NOT EXISTS idx_patch_failures_agent_id
    ON public.patch_failures(agent_id);

CREATE INDEX IF NOT EXISTS idx_patch_failures_patch_id
    ON public.patch_failures(patch_id);

CREATE INDEX IF NOT EXISTS idx_patch_failures_severity
    ON public.patch_failures(severity);

CREATE INDEX IF NOT EXISTS idx_patch_failures_recovery_status
    ON public.patch_failures(recovery_status);

-- === (Optional) Foreign Key Constraint (uncomment if agent_registry exists) ===
-- ALTER TABLE public.patch_failures
--     ADD CONSTRAINT fk_patch_failures_agent
--     FOREIGN KEY (agent_id) REFERENCES public.agent_registry(id)
--     ON DELETE SET NULL;

-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- CREATE OR REPLACE FUNCTION update_patch_failures_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
--
-- DROP TRIGGER IF EXISTS trg_update_patch_failures_updated_at ON public.patch_failures;
-- CREATE TRIGGER trg_update_patch_failures_updated_at
--     BEFORE UPDATE ON public.patch_failures
--     FOR EACH ROW EXECUTE FUNCTION update_patch_failures_updated_at();

-- === Row-Level Security Policy Example (Optional) ===
-- ALTER TABLE public.patch_failures ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY patch_failures_agent_access
--     ON public.patch_failures
--     USING (agent_id IS NULL OR agent_id = current_setting('app.current_agent_id')::integer);

-- === Example Insert for Testing ===

INSERT INTO public.patch_failures
    (agent_id, patch_id, error_message, error_details, severity, recovery_status)
VALUES
    (1, 'patch_20240706_001', 'Failed to apply schema migration: missing column', '{"trace":"KeyError: column not found"}', 'error', 'pending');

-- === End Migration ===

COMMIT;

COMMIT;
