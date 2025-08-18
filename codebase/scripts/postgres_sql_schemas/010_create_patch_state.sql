-- scripts/migrations/010_create_patch_state.sql
-- Version: 2.0
-- Created: 2025-07-07
-- Description: Schema for patch lifecycle key-value state store (replaces patch_state.json)

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.patch_state (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    value_type VARCHAR(32) NOT NULL DEFAULT 'jsonb',
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    notes TEXT DEFAULT NULL,
    created_by VARCHAR(64) DEFAULT 'system',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.patch_state IS
    'Stores key-value state for the patch system (e.g. lock flags, last_applied ID). Replaces patch_state.json flat file.';

COMMENT ON COLUMN public.patch_state.key IS
    'Primary identifier for the state record. Examples: "lock", "last_applied", "agent_context".';

COMMENT ON COLUMN public.patch_state.value IS
    'Arbitrary structured value stored as JSONB. Used to persist boolean flags, patch metadata, or goal/task references.';

COMMENT ON COLUMN public.patch_state.value_type IS
    'Describes expected value type (e.g. jsonb, boolean, string, list, object). Supports validation or schema introspection.';

COMMENT ON COLUMN public.patch_state.status IS
    'Logical status of this state key. Defaults to "active". Reserved values: active, deprecated, frozen, flagged.';

COMMENT ON COLUMN public.patch_state.notes IS
    'Optional internal notes about this state key: usage, last mutation reason, or migration history.';

COMMENT ON COLUMN public.patch_state.created_by IS
    'Actor that created the row (agent ID, system, CLI). Supports audit chain.';

COMMENT ON COLUMN public.patch_state.created_at IS
    'Timestamp of row creation. Immutable audit marker.';

COMMENT ON COLUMN public.patch_state.updated_at IS
    'Timestamp of most recent update. Used by triggers or daemon sync.';

-- === Indexes (in addition to PK) ===

CREATE INDEX IF NOT EXISTS idx_patch_state_status
    ON public.patch_state(status);

CREATE INDEX IF NOT EXISTS idx_patch_state_type
    ON public.patch_state(value_type);

-- === Audit Trigger for updated_at (Optional) ===
-- CREATE OR REPLACE FUNCTION trg_update_patch_state_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
--
-- DROP TRIGGER IF EXISTS trg_patch_state_timestamp ON public.patch_state;
-- CREATE TRIGGER trg_patch_state_timestamp
--     BEFORE UPDATE ON public.patch_state
--     FOR EACH ROW EXECUTE FUNCTION trg_update_patch_state_updated_at();

-- === (Optional) RLS Support ===
-- ALTER TABLE public.patch_state ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY patch_state_agent_isolation
--     ON public.patch_state
--     USING (created_by = current_setting('app.current_agent_id'));

-- === Example Insert for Testing ===

INSERT INTO public.patch_state (key, value, value_type, status, created_by)
VALUES
    ('lock', 'false'::jsonb, 'boolean', 'active', 'system');

-- === End Migration ===

COMMIT;

COMMIT;
