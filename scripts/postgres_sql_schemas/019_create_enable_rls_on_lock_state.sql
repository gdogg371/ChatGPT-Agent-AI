-- scripts/migrations/019_enable_rls_on_lock_state.sql

BEGIN;

ALTER TABLE public.lock_state ENABLE ROW LEVEL SECURITY;

CREATE POLICY lock_owner_only
    ON public.lock_state
    USING (holder = current_setting('app.current_actor')::text);

COMMENT ON POLICY lock_owner_only ON public.lock_state IS
    'Only allow an agent to see its own locks by holder tag.';

COMMIT;
