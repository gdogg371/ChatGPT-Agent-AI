-- scripts/migrations/017_lock_state.sql
-- Version: 2.0
-- Created: 2024-07-10
-- Description: Extended lock_state schema with TTL, expiry, concurrency policy, and RLS hooks

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.lock_state (
    lock_name VARCHAR(64) PRIMARY KEY,
    value BOOLEAN NOT NULL DEFAULT FALSE,
    holder VARCHAR(128),
    since TIMESTAMP,
    ttl_seconds INTEGER DEFAULT 900 CHECK (ttl_seconds > 0),
    expires_at TIMESTAMP GENERATED ALWAYS AS (
        CASE
            WHEN since IS NOT NULL AND ttl_seconds IS NOT NULL THEN
                since + (ttl_seconds || ' seconds')::interval
            ELSE NULL
        END
    ) STORED,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.lock_state IS
    'Enhanced lock coordination table for agent and CLI concurrency safety. Includes TTL, expiry calculation, metadata, and audit capability.';

COMMENT ON COLUMN public.lock_state.lock_name IS
    'Unique string name for the lock (e.g. patch_lock, planner_lock).';
COMMENT ON COLUMN public.lock_state.value IS
    'TRUE = lock held, FALSE = lock released.';
COMMENT ON COLUMN public.lock_state.holder IS
    'Who is holding the lock — agent name, CLI user, or daemon.';
COMMENT ON COLUMN public.lock_state.since IS
    'Timestamp the lock was most recently acquired.';
COMMENT ON COLUMN public.lock_state.ttl_seconds IS
    'Time-to-live (in seconds) after which the lock is considered expired.';
COMMENT ON COLUMN public.lock_state.expires_at IS
    'Automatically calculated expiry timestamp based on since + ttl_seconds.';
COMMENT ON COLUMN public.lock_state.metadata IS
    'Flexible JSON for tags, lock type, retries, notes.';
COMMENT ON COLUMN public.lock_state.created_at IS
    'Row creation time — not modifiable.';
COMMENT ON COLUMN public.lock_state.updated_at IS
    'Last update time — should update on change.';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_lock_state_active
    ON public.lock_state(value, expires_at);

CREATE INDEX IF NOT EXISTS idx_lock_state_holder
    ON public.lock_state(holder);

-- === Audit Trigger for updated_at (Optional) ===

-- CREATE OR REPLACE FUNCTION update_lock_state_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;

-- DROP TRIGGER IF EXISTS trg_lock_state_updated_at ON public.lock_state;
-- CREATE TRIGGER trg_lock_state_updated_at
--     BEFORE UPDATE ON public.lock_state
--     FOR EACH ROW EXECUTE FUNCTION update_lock_state_updated_at();

-- === Row Level Security (Optional) ===

-- ALTER TABLE public.lock_state ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY lock_visibility_policy
--     ON public.lock_state
--     USING (holder = current_setting('app.current_actor')::text);

-- === Bootstrap Lock Definitions ===

INSERT INTO public.lock_state (lock_name, value, holder, ttl_seconds)
VALUES
    ('patch_lock', FALSE, NULL, 600),
    ('planner_lock', FALSE, NULL, 600),
    ('goal_lock_agent_1', FALSE, NULL, 900)
ON CONFLICT (lock_name) DO NOTHING;

-- === End Migration ===

COMMIT;
COMMIT;
