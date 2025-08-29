-- Version: 3.1
-- Created: 2025-08-12
-- Description: Hardened schema for agent_insights with dedupe and JSON validation
BEGIN;

CREATE TABLE IF NOT EXISTS agent_insights (
    id                INTEGER PRIMARY KEY,
    agent_id          INTEGER NOT NULL,
    insight_type      TEXT    NOT NULL,
    content           TEXT    NOT NULL,
    source            TEXT    DEFAULT 'internal',
    score             REAL    CHECK (score >= 0.0 AND score <= 1.0),
    mdata             TEXT    DEFAULT '{}' CHECK (json_valid(mdata)),

    -- Location & lifecycle
    filepath          TEXT,
    symbol_name       TEXT,
    line_number       INTEGER DEFAULT 0,
    unique_key_hash   TEXT,
    status            TEXT    NOT NULL DEFAULT 'active',
    discovered_at     TEXT    DEFAULT CURRENT_TIMESTAMP,
    last_seen_at      TEXT    DEFAULT CURRENT_TIMESTAMP,
    resolved_at       TEXT,
    occurrences       INTEGER DEFAULT 1,
    recurrence_count  INTEGER DEFAULT 0,
    created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed          INTEGER NOT NULL DEFAULT 0,  -- boolean
    reviewer          TEXT,
    review_comment    TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_insights_agent_id ON agent_insights(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_insights_type     ON agent_insights(insight_type);
CREATE INDEX IF NOT EXISTS idx_agent_insights_reviewed ON agent_insights(reviewed);
CREATE INDEX IF NOT EXISTS idx_agent_insights_key_hash ON agent_insights(unique_key_hash);

-- Dedupe guarantee per agent + stable key
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_insights_agent_key
  ON agent_insights(agent_id, unique_key_hash);

COMMIT;

