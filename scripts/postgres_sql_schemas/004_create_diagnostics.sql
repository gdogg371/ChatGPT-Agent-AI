-- scripts/migrations/004_create_diagnostics.sql
-- Version: 3.0
-- Created: 2025-08-03
-- Description: Diagnostics table for agent/system health checks, error logs, and traceable events with lifecycle tracking

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.diagnostics (
    id SERIAL PRIMARY KEY,
    agent_id INTEGER,
    event_type VARCHAR(64) NOT NULL,
    event_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    severity VARCHAR(32) NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    details JSONB DEFAULT '{}'::jsonb,
    source VARCHAR(128),

    filepath TEXT,
    symbol_name VARCHAR(128),
    line_number INTEGER DEFAULT -1,

    -- === Lifecycle & Deduplication Tracking ===
    unique_key_hash TEXT,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP,
    occurrences INTEGER NOT NULL DEFAULT 1,
    recurrence_count INTEGER NOT NULL DEFAULT 0,

    resolution TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Documentation and Traceability ===

COMMENT ON TABLE public.diagnostics IS
    'Persistent diagnostics, health check results, system errors, and traceable agent events. Includes lifecycle tracking for analyzers and introspective scans.';

COMMENT ON COLUMN public.diagnostics.id IS
    'Primary key for diagnostics events.';
COMMENT ON COLUMN public.diagnostics.agent_id IS
    'Optional: associated agent, if applicable (FK to agent_registry).';
COMMENT ON COLUMN public.diagnostics.event_type IS
    'Type/category of event (e.g., error, health_check, heartbeat, warning, audit, recovery).';
COMMENT ON COLUMN public.diagnostics.event_time IS
    'When the event occurred or was detected.';
COMMENT ON COLUMN public.diagnostics.severity IS
    'Log/event severity: info, warning, error, critical, etc.';
COMMENT ON COLUMN public.diagnostics.message IS
    'Short summary of the event (for dashboards, alerting).';
COMMENT ON COLUMN public.diagnostics.details IS
    'Structured JSONB details (stack traces, environment, etc).';
COMMENT ON COLUMN public.diagnostics.source IS
    'Process, subsystem, script, or module reporting the event.';
COMMENT ON COLUMN public.diagnostics.filepath IS
    'Full file path to the source of the event (scanner output).';
COMMENT ON COLUMN public.diagnostics.symbol_name IS
    'Function or class name associated with the event.';
COMMENT ON COLUMN public.diagnostics.line_number IS
    'Line number in the file, if applicable.';
COMMENT ON COLUMN public.diagnostics.unique_key_hash IS
    'Deterministic hash of event identity for deduplication across scans.';
COMMENT ON COLUMN public.diagnostics.status IS
    'Event/lifecycle status: active, resolved, ignored, etc.';
COMMENT ON COLUMN public.diagnostics.discovered_at IS
    'First time this issue was observed.';
COMMENT ON COLUMN public.diagnostics.last_seen_at IS
    'Most recent scan where this issue was still present.';
COMMENT ON COLUMN public.diagnostics.resolved_at IS
    'If resolved, when it disappeared from scan output.';
COMMENT ON COLUMN public.diagnostics.occurrences IS
    'Total number of times this issue has been detected.';
COMMENT ON COLUMN public.diagnostics.recurrence_count IS
    'Number of times this issue was resolved and then reappeared.';
COMMENT ON COLUMN public.diagnostics.resolution IS
    'If resolved, summary of mitigation/response.';
COMMENT ON COLUMN public.diagnostics.created_at IS
    'Row creation timestamp.';
COMMENT ON COLUMN public.diagnostics.updated_at IS
    'Last update timestamp.';

-- === Indexes for Performance ===

CREATE INDEX IF NOT EXISTS idx_diagnostics_agent_id
    ON public.diagnostics(agent_id);

CREATE INDEX IF NOT EXISTS idx_diagnostics_event_type
    ON public.diagnostics(event_type);

CREATE INDEX IF NOT EXISTS idx_diagnostics_severity
    ON public.diagnostics(severity);

CREATE INDEX IF NOT EXISTS idx_diagnostics_status
    ON public.diagnostics(status);

CREATE INDEX IF NOT EXISTS idx_diagnostics_unique_key_hash
    ON public.diagnostics(unique_key_hash);

-- === (Optional) Foreign Key Constraint (uncomment if agent_registry exists) ===
-- ALTER TABLE public.diagnostics
--     ADD CONSTRAINT fk_diagnostics_agent
--     FOREIGN KEY (agent_id) REFERENCES public.agent_registry(id)
--     ON DELETE SET NULL;

-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- CREATE OR REPLACE FUNCTION update_diagnostics_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
--
-- DROP TRIGGER IF EXISTS trg_update_diagnostics_updated_at ON public.diagnostics;
-- CREATE TRIGGER trg_update_diagnostics_updated_at
--     BEFORE UPDATE ON public.diagnostics
--     FOR EACH ROW EXECUTE FUNCTION update_diagnostics_updated_at();

-- === Row-Level Security Policy Example (Optional) ===
-- ALTER TABLE public.diagnostics ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY diagnostics_agent_access
--     ON public.diagnostics
--     USING (agent_id IS NULL OR agent_id = current_setting('app.current_agent_id')::integer);

-- === Example Insert for Testing ===

INSERT INTO public.diagnostics
    (agent_id, event_type, severity, message, details, status, unique_key_hash, discovered_at, last_seen_at, occurrences, recurrence_count, filepath, symbol_name, line_number)
VALUES
    (1, 'filesystem_scan', 'warning', 'Suspicious file in __pycache__', '{"file":"__init__.cpython-312.pyc"}', 'active',
     'abc123def456', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1, 0,
     'C:\\Users\\cg371\\PycharmProjects\\ChatGPT Bot\\backend\\core\\messaging\\__pycache__\\__init__.cpython-312.pyc',
     'check_nonstandard_extensions', -1);

-- === End Migration ===

COMMIT;

COMMIT;
