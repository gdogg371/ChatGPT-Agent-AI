-- scripts/migrations/002_create_patch_state.sql
-- Version: 2.0
-- Created: 2024-07-12
-- Description: Schema for persistent patch state tracking (replaces patch_state.json)

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.patch_state (
    id SERIAL PRIMARY KEY,
    last_patch TEXT,
    active BOOLEAN NOT NULL DEFAULT FALSE,
    lock BOOLEAN NOT NULL DEFAULT FALSE,
    error TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.patch_state IS
    'Stores the current and historical patch application state for the system. Replaces patch_state.json.';

COMMENT ON COLUMN public.patch_state.id IS
    'Primary key for the patch state entry (usually only one row with id=1).';

COMMENT ON COLUMN public.patch_state.last_patch IS
    'Name or ID of the last successfully applied patch. Used for audit and CI checkpointing.';

COMMENT ON COLUMN public.patch_state.active IS
    'Boolean flag indicating whether a patch is currently in progress (true = mid-apply).';

COMMENT ON COLUMN public.patch_state.lock IS
    'Boolean lock flag — used to prevent concurrent patch runs.';

COMMENT ON COLUMN public.patch_state.error IS
    'Text description of the last known patch error, if any.';

COMMENT ON COLUMN public.patch_state.timestamp IS
    'Last updated logical time (e.g. end of patch, failure point).';

COMMENT ON COLUMN public.patch_state.metadata IS
    'Flexible JSONB field for patch agent ID, hostname, environment, etc.';

COMMENT ON COLUMN public.patch_state.created_at IS
    'When this row was first created (system init).';

COMMENT ON COLUMN public.patch_state.updated_at IS
    'When this row was last modified. Updated on every patch state change.';

-- === Indexes ===

CREATE UNIQUE INDEX IF NOT EXISTS idx_patch_state_singleton
    ON public.patch_state((id)) WHERE id = 1;

CREATE INDEX IF NOT EXISTS idx_patch_state_active
    ON public.patch_state(active);

-- === Optional RLS Policy (Single-Agent Isolation) ===
-- ALTER TABLE public.patch_state ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY patch_state_isolation
--     ON public.patch_state
--     USING (metadata->>'agent_id' = current_setting('app.current_agent_id'));

-- === Audit Trigger for updated_at (Optional) ===
-- CREATE OR REPLACE FUNCTION update_patch_state_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
--
-- DROP TRIGGER IF EXISTS trg_update_patch_state_updated_at ON public.patch_state;
-- CREATE TRIGGER trg_update_patch_state_updated_at
--     BEFORE UPDATE ON public.patch_state
--     FOR EACH ROW EXECUTE FUNCTION update_patch_state_updated_at();

-- === Example Insert for Bootstrapping ===

INSERT INTO public.patch_state
    (last_patch, active, lock, error, metadata)
VALUES
    (NULL, FALSE, FALSE, NULL, '{"agent_id": "bootstrap", "env": "dev"}');

-- === End Migration ===

COMMIT;

COMMIT;
