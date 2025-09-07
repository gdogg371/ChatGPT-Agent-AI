-- scripts/migrations/012_create_patch_actions.sql
-- Version: 2.0
-- Created: 2025-07-07
-- Description: Schema for audit-log of granular patch actions (e.g., per step inside a patch plan)

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.patch_actions (
    id SERIAL PRIMARY KEY,
    patch_id VARCHAR(128) NOT NULL,
    action_index INTEGER NOT NULL CHECK (action_index >= 0),
    action_type VARCHAR(64) NOT NULL CHECK (
        action_type IN ('copy', 'replace', 'delete', 'exec', 'noop')
    ),
    target TEXT NOT NULL,
    source TEXT,
    status VARCHAR(64) DEFAULT 'pending',
    result TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    agent_id INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.patch_actions IS
    'Logs each action performed during a patch execution. Enables granular diagnostics, rollback targeting, and CI pipeline testing.';

COMMENT ON COLUMN public.patch_actions.id IS
    'Unique ID for each individual patch action recorded.';

COMMENT ON COLUMN public.patch_actions.patch_id IS
    'Patch this action belongs to. Joins to patch_history.patch_id (1:N).';

COMMENT ON COLUMN public.patch_actions.action_index IS
    'Position of the action in the patch plan sequence (0-indexed).';

COMMENT ON COLUMN public.patch_actions.action_type IS
    'Declared operation: copy, replace, delete, exec, or noop.';

COMMENT ON COLUMN public.patch_actions.target IS
    'Filesystem or service target of the patch action (e.g., path, ID, config key).';

COMMENT ON COLUMN public.patch_actions.source IS
    'Optional source path or data (used in copy/replace).';

COMMENT ON COLUMN public.patch_actions.status IS
    'Execution status: pending, success, failed, skipped.';

COMMENT ON COLUMN public.patch_actions.result IS
    'Optional human/agent-readable output from the action. E.g. return code, log msg.';

COMMENT ON COLUMN public.patch_actions.metadata IS
    'Structured metadata (e.g., SHA256, duration, warnings).';

COMMENT ON COLUMN public.patch_actions.agent_id IS
    'ID of the agent that executed this action (if applicable).';

COMMENT ON COLUMN public.patch_actions.created_at IS
    'Timestamp of action execution logging.';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_patch_actions_patch_id
    ON public.patch_actions(patch_id);

CREATE INDEX IF NOT EXISTS idx_patch_actions_status
    ON public.patch_actions(status);

-- === Optional FK (deferred constraint to allow log-first) ===
-- ALTER TABLE public.patch_actions
-- ADD CONSTRAINT fk_patch_action_history
-- FOREIGN KEY (patch_id) REFERENCES public.patch_history(patch_id)
-- ON DELETE CASCADE;

-- === Optional RLS ===
-- ALTER TABLE public.patch_actions ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY patch_action_agent_filter
--     ON public.patch_actions
--     USING (agent_id = current_setting('app.current_agent_id')::int);

-- === Example Insert for Testing ===

INSERT INTO public.patch_actions
    (patch_id, action_index, action_type, target, source, status, result, agent_id)
VALUES
    ('2025_07_initial_patch', 0, 'copy', '/etc/config.yaml', '/tmp/config.yaml', 'success', 'Overwritten OK', 1);

-- === End Migration ===

COMMIT;
