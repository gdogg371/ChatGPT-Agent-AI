-- scripts/migrations/004_create_diagnostics.sql
-- Version: 3.0
-- Created: 2025-08-03
-- Description: Diagnostics table for agent/system health checks, error logs, and traceable events with lifecycle tracking
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS diagnostics (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER,
    event_type TEXT NOT NULL,
    event_time TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    severity TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    details TEXT DEFAULT '{}',
    source TEXT,
    filepath TEXT,
    symbol_name TEXT,
    line_number INTEGER DEFAULT -1,
    -- === Lifecycle & Deduplication Tracking ===
    unique_key_hash TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    discovered_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT,
    occurrences INTEGER NOT NULL DEFAULT 1,
    recurrence_count INTEGER NOT NULL DEFAULT 0,
    resolution TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Documentation and Traceability ===
-- === Indexes for Performance ===
CREATE INDEX IF NOT EXISTS idx_diagnostics_agent_id
    ON diagnostics(agent_id);
CREATE INDEX IF NOT EXISTS idx_diagnostics_event_type
    ON diagnostics(event_type);
CREATE INDEX IF NOT EXISTS idx_diagnostics_severity
    ON diagnostics(severity);
CREATE INDEX IF NOT EXISTS idx_diagnostics_status
    ON diagnostics(status);
CREATE INDEX IF NOT EXISTS idx_diagnostics_unique_key_hash
    ON diagnostics(unique_key_hash);
-- === (Optional) Foreign Key Constraint (uncomment if agent_registry exists) ===
-- ALTER TABLE diagnostics
--     ADD CONSTRAINT fk_diagnostics_agent
--     FOREIGN KEY (agent_id) REFERENCES agent_registry(id)
--     ON DELETE SET NULL;
-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- 
--
-- DROP TRIGGER IF EXISTS trg_update_diagnostics_updated_at ON diagnostics;
-- 
-- === Row-Level Security Policy Example (Optional) ===
-- 
-- 
-- === Example Insert for Testing ===
INSERT INTO diagnostics
    (agent_id, event_type, severity, message, details, status, unique_key_hash, discovered_at, last_seen_at, occurrences, recurrence_count, filepath, symbol_name, line_number)
VALUES
    (1, 'filesystem_scan', 'warning', 'Suspicious file in __pycache__', '{"file":"__init__.cpython-312.pyc"}', 'active',
     'abc123def456', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1, 0,
     'C:\\Users\\cg371\\PycharmProjects\\ChatGPT Bot\\backend\\core\\messaging\\__pycache__\\__init__.cpython-312.pyc',
     'check_nonstandard_extensions', -1);
-- === End Migration ===
COMMIT;
COMMIT;
