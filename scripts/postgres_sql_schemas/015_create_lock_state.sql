-- scripts/migrations/015_lock_state.sql
-- Version: 2.0
-- Created: 2024-07-10
-- Description: Schema for DB-backed system-wide locks (e.g. patch_lock, agent_lock, planner_lock)

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.lock_state (
    lock_name VARCHAR(64) PRIMARY KEY,
    value BOOLEAN NOT NULL DEFAULT FALSE,
    holder VARCHAR(128),
    since TIMESTAMP,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.lock_state IS
    'Tracks named logical locks for concurrency control across agent components (e.g. patch_lock, planner_lock, goal_lock). Ensures safe coordination of patching, planning, and reflection.';

COMMENT ON COLUMN public.lock_state.lock_name IS
    'Lock identifier (e.g. patch_lock, agent_3_goal_lock). Unique and acts as primary key.';
COMMENT ON COLUMN public.lock_state.value IS
    'Boolean flag — TRUE if the lock is currently held.';
COMMENT ON COLUMN public.lock_state.holder IS
    'Textual label of the actor holding the lock (e.g. "agent/2", "cli", "loop-daemon").';
COMMENT ON COLUMN public.lock_state.since IS
    'Timestamp of the last lock acquisition (NULL if never locked).';
COMMENT ON COLUMN public.lock_state.metadata IS
    'JSON blob for auxiliary data: who requested the lock, expiry hints, flags, etc.';
COMMENT ON COLUMN public.lock_state.created_at IS
    'Creation timestamp (set once at row creation).';
COMMENT ON COLUMN public.lock_state.updated_at IS
    'Last-modified timestamp — should update on every change.';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_lock_state_value
    ON public.lock_state(value);

CREATE INDEX IF NOT EXISTS idx_lock_state_holder
    ON public.lock_state(holder);

-- === Audit Trigger for updated_at ===

-- CREATE OR REPLACE FUNCTION update_lock_state_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;

-- DROP TRIGGER IF EXISTS trg_update_lock_state_updated_at ON public.lock_state;
-- CREATE TRIGGER trg_update_lock_state_updated_at
--     BEFORE UPDATE ON public.lock_state
--     FOR EACH ROW EXECUTE FUNCTION update_lock_state_updated_at();

-- === Row Level Security (Optional Example) ===

-- ALTER TABLE public.lock_state ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY lock_isolation
--     ON public.lock_state
--     USING (holder = current_setting('app.current_actor')::text);

-- === Initial Lock Definitions ===

INSERT INTO public.lock_state (lock_name, value, holder)
VALUES
    ('patch_lock', FALSE, NULL),
    ('planner_lock', FALSE, NULL),
    ('goal_lock_agent_1', FALSE, NULL)
ON CONFLICT (lock_name) DO NOTHING;

-- === End Migration ===

COMMIT;
COMMIT;
