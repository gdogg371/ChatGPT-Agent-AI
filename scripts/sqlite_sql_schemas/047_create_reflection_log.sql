-- scripts/migrations/002_create_reflection_log.sql
-- Version: 1.0
-- Created: 2025-07-23
-- Description: SQLite schema for reflection_log (self-review event log)
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS reflection_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    check_type TEXT NOT NULL,
    outcome TEXT NOT NULL,
    message TEXT
);
-- === Comments for Full Traceability ===
-- SQLite does not support 
CREATE INDEX IF NOT EXISTS idx_reflection_log_outcome
    ON reflection_log(outcome);
-- === Example Insert for Testing ===
INSERT INTO reflection_log (check_type, outcome, message)
VALUES ('patch_state_check', 'pass', 'Patch state valid and consistent');
-- === End Migration ===
COMMIT;
