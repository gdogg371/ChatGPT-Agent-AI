-- scripts/migrations/018_create_lock_events.sql

BEGIN;

CREATE TABLE IF NOT EXISTS public.lock_events (
    id SERIAL PRIMARY KEY,
    lock_name VARCHAR(64) NOT NULL,
    action VARCHAR(32) NOT NULL CHECK (action IN ('acquire', 'release', 'fail')),
    holder VARCHAR(128),
    metadata JSONB DEFAULT '{}'::jsonb,
    event_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

COMMENT ON TABLE public.lock_events IS 'Audit log of all lock actions for traceability.';
COMMENT ON COLUMN public.lock_events.lock_name IS 'The lock involved (e.g. patch_lock).';
COMMENT ON COLUMN public.lock_events.action IS 'What happened: acquire, release, or fail.';
COMMENT ON COLUMN public.lock_events.holder IS 'Who/what held or attempted the lock.';
COMMENT ON COLUMN public.lock_events.metadata IS 'Extra trace data for debugging or audit.';
COMMENT ON COLUMN public.lock_events.event_time IS 'When the event occurred.';

CREATE INDEX IF NOT EXISTS idx_lock_events_name ON public.lock_events(lock_name);
CREATE INDEX IF NOT EXISTS idx_lock_events_time ON public.lock_events(event_time);

COMMIT;
