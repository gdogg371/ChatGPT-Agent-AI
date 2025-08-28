-- scripts/migrations/011_create_patch_history.sql
-- Version: 2.0
-- Created: 2025-07-07
-- Description: Schema for patch event audit log (applied/rollback/failure trace)

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.patch_history (
    id SERIAL PRIMARY KEY,
    patch_id VARCHAR(128) NOT NULL,
    status VARCHAR(64) NOT NULL CHECK (
        status IN ('applied', 'rolled_back', 'failed', 'recovered', 'planned')
    ),
    agent_id INTEGER,
    actor VARCHAR(64) DEFAULT 'system',
    metadata JSONB DEFAULT '{}'::jsonb,
    notes TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.patch_history IS
    'Tracks each patch execution event: apply, rollback, fail, recover. Supports audit, CI, RAG-based safety, and planner evaluation.';

COMMENT ON COLUMN public.patch_history.id IS
    'Unique auto-incrementing ID for the patch event log entry.';

COMMENT ON COLUMN public.patch_history.patch_id IS
    'External patch identifier or filename. Matches patch JSON metadata.';

COMMENT ON COLUMN public.patch_history.status IS
    'Event status: applied, rolled_back, failed, recovered, or planned. Defines patch state transition.';

COMMENT ON COLUMN public.patch_history.agent_id IS
    'ID of the agent that initiated the patch, if known.';

COMMENT ON COLUMN public.patch_history.actor IS
    'Textual actor tag (system, planner, CLI, agent-42). Used for debugging or audit segmentation.';

COMMENT ON COLUMN public.patch_history.metadata IS
    'Flexible structured metadata (version, target env, duration, error trace). Stored as JSONB.';

COMMENT ON COLUMN public.patch_history.notes IS
    'Freeform comment or explanation of patch outcome. Used by agents or humans.';

COMMENT ON COLUMN public.patch_history.created_at IS
    'Timestamp when this patch event was logged. Immutable.';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_patch_history_patch_id
    ON public.patch_history(patch_id);

CREATE INDEX IF NOT EXISTS idx_patch_history_status
    ON public.patch_history(status);

CREATE INDEX IF NOT EXISTS idx_patch_history_agent
    ON public.patch_history(agent_id);

-- === RLS Policy (Optional) ===
-- ALTER TABLE public.patch_history ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY patch_history_agent_view
--     ON public.patch_history
--     USING (agent_id = current_setting('app.current_agent_id')::int);

-- === Example Insert for Testing ===

INSERT INTO public.patch_history
    (patch_id, status, agent_id, actor, metadata, notes)
VALUES
    ('2025_07_initial_patch', 'applied', 1, 'system', '{"duration": "5s"}', 'Initial bootstrapping patch applied successfully.');

-- === End Migration ===

COMMIT;
