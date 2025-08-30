-- scripts/migrations/002_create_system_events.sql
-- Version: 2.0
-- Created: 2025-07-19
-- Description: Schema for system_events — internal event logging for diagnostics, health checks, and agent telemetry

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.system_events (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source VARCHAR(128) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    details TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.system_events IS
    'System-wide diagnostics, health check, and event audit log — supports internal telemetry and error tracking.';

COMMENT ON COLUMN public.system_events.id IS
    'Primary key (autoincrementing event record ID).';
COMMENT ON COLUMN public.system_events.timestamp IS
    'Event occurrence time (UTC).';
COMMENT ON COLUMN public.system_events.source IS
    'Subsystem or module reporting the event (e.g. "clock_drift", "watchdog", "agent_loop").';
COMMENT ON COLUMN public.system_events.event_type IS
    'Type/classification of the event (e.g. "drift_alert", "startup", "error").';
COMMENT ON COLUMN public.system_events.details IS
    'Freeform event description or error message, intended for audit/debug.';
COMMENT ON COLUMN public.system_events.created_at IS
    'Row creation time (typically same as timestamp, but allows for delayed insert).';
COMMENT ON COLUMN public.system_events.updated_at IS
    'Timestamp of last update (auto-managed via trigger if enabled).';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_system_events_timestamp
    ON public.system_events(timestamp);

CREATE INDEX IF NOT EXISTS idx_system_events_source
    ON public.system_events(source);

CREATE INDEX IF NOT EXISTS idx_system_events_event_type
    ON public.system_events(event_type);

-- === (Optional) Audit Trigger for updated_at ===

-- CREATE OR REPLACE FUNCTION update_system_events_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
--
-- DROP TRIGGER IF EXISTS trg_update_system_events_updated_at ON public.system_events;
-- CREATE TRIGGER trg_update_system_events_updated_at
--     BEFORE UPDATE ON public.system_events
--     FOR EACH ROW EXECUTE FUNCTION update_system_events_updated_at();

-- === Row Level Security Policy (Optional Example) ===
-- ALTER TABLE public.system_events ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY system_events_visibility
--     ON public.system_events
--     USING (true);  -- Replace with scoped condition if needed

-- === Example Insert for Testing ===

INSERT INTO public.system_events
    (timestamp, source, event_type, details)
VALUES
    (NOW(), 'clock_drift', 'drift_alert', 'Drift exceeded 600 seconds');

-- === End Migration ===

COMMIT;

COMMIT;
