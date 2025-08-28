-- scripts/migrations/NNN_create_patch_action_log.sql
-- Version: 2.0
-- Created: 2025-07-20
-- Description: Schema for logging patch action execution results and metadata

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.patch_action_log (
    id SERIAL PRIMARY KEY,
    patch_id VARCHAR(128) NOT NULL,
    action_type VARCHAR(64) NOT NULL,
    target TEXT,
    payload_preview TEXT,
    status VARCHAR(32) NOT NULL CHECK (status IN ('success', 'failure', 'unsupported', 'exception')),
    message TEXT,
    execution_order INTEGER DEFAULT 0,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metadata JSONB DEFAULT '{}'::jsonb
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.patch_action_log IS
    'Logs each action executed as part of a patch plan — file writes, deletes, shell commands — including outcomes and audit messages.';

COMMENT ON COLUMN public.patch_action_log.id IS
    'Primary key for each action log entry.';
COMMENT ON COLUMN public.patch_action_log.patch_id IS
    'Identifier of the patch this action belongs to.';
COMMENT ON COLUMN public.patch_action_log.action_type IS
    'Type of action executed: write, delete, shell.';
COMMENT ON COLUMN public.patch_action_log.target IS
    'Target file or path affected by the action.';
COMMENT ON COLUMN public.patch_action_log.payload_preview IS
    'Optional preview of the action payload (e.g. snippet of written text or shell command).';
COMMENT ON COLUMN public.patch_action_log.status IS
    'Outcome of the action: success, failure, unsupported, or exception.';
COMMENT ON COLUMN public.patch_action_log.message IS
    'Diagnostic or success/failure message associated with this action.';
COMMENT ON COLUMN public.patch_action_log.execution_order IS
    'Optional execution sequence number for ordering actions within a patch.';
COMMENT ON COLUMN public.patch_action_log.timestamp IS
    'UTC timestamp when the action was executed.';
COMMENT ON COLUMN public.patch_action_log.metadata IS
    'JSONB field for future extensibility — diagnostics, metrics, etc.';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_patch_action_log_patch_id
    ON public.patch_action_log(patch_id);

CREATE INDEX IF NOT EXISTS idx_patch_action_log_status
    ON public.patch_action_log(status);

CREATE INDEX IF NOT EXISTS idx_patch_action_log_timestamp
    ON public.patch_action_log(timestamp);

-- === Example Insert for Testing ===

INSERT INTO public.patch_action_log
    (patch_id, action_type, target, payload_preview, status, message, execution_order)
VALUES
    ('test_patch_001', 'write', '/tmp/example.txt', 'Hello World', 'success', 'Wrote to example.txt', 1);

-- === End Migration ===

COMMIT;
