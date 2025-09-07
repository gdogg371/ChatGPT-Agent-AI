-- scripts/migrations/002_create_system_events.sql
-- Version: 2.0
-- Created: 2025-07-19
-- Description: Schema for system_events — internal event logging for diagnostics, health checks, and agent telemetry
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_system_events_timestamp
    ON system_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_system_events_source
    ON system_events(source);
CREATE INDEX IF NOT EXISTS idx_system_events_event_type
    ON system_events(event_type);
-- === (Optional) Audit Trigger for updated_at ===
-- 
--
-- DROP TRIGGER IF EXISTS trg_update_system_events_updated_at ON system_events;
-- 
-- === Row Level Security Policy (Optional Example) ===
-- 
--   -- Replace with scoped condition if needed
-- === Example Insert for Testing ===
INSERT INTO system_events
    (timestamp, source, event_type, details)
VALUES
    (NOW(), 'clock_drift', 'drift_alert', 'Drift exceeded 600 seconds');
-- === End Migration ===
COMMIT;
COMMIT;
