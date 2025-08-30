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
-- SQLite does not support COMMENT ON directly, but notes provided here for clarity:

-- Table: reflection_log
-- Purpose: Logs self-review events emitted by the agent's introspection and readiness functions.

-- Column: id
-- Description: Autoincrementing primary key for each reflection event.

-- Column: timestamp
-- Description: ISO timestamp of when the reflection event occurred.

-- Column: check_type
-- Description: Label or category of the self-check performed (e.g. "patch_state_check").

-- Column: outcome
-- Description: Outcome of the check (e.g. "pass", "fail", "warning").

-- Column: message
-- Description: Optional detailed explanation or debug context.

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_reflection_log_check_type
    ON reflection_log(check_type);

CREATE INDEX IF NOT EXISTS idx_reflection_log_outcome
    ON reflection_log(outcome);

-- === Example Insert for Testing ===

INSERT INTO reflection_log (check_type, outcome, message)
VALUES ('patch_state_check', 'pass', 'Patch state valid and consistent');

-- === End Migration ===

COMMIT;
