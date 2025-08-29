-- Version: 2.1
-- Created: 2025-08-12
-- Description: Hardened schema for introspection_index with dedupe and indices
BEGIN;

CREATE TABLE IF NOT EXISTS introspection_index (
    id                INTEGER PRIMARY KEY,
    filepath          TEXT    NOT NULL,
    symbol_type       TEXT    NOT NULL DEFAULT 'unknown',  -- module|class|function|route|unknown
    name              TEXT,
    lineno            INTEGER DEFAULT 0,
    route_method      TEXT,
    route_path        TEXT,
    ag_tag            TEXT,
    description       TEXT,
    target_symbol     TEXT,  -- for relations (calls/imports/inherits)
    relation_type     TEXT,

    -- Lifecycle / identity
    unique_key_hash   TEXT,                                -- deterministic hash for dedupe
    status            TEXT    NOT NULL DEFAULT 'active',   -- active|deprecated|removed
    discovered_at     TEXT    DEFAULT CURRENT_TIMESTAMP,
    last_seen_at      TEXT    DEFAULT CURRENT_TIMESTAMP,
    resolved_at       TEXT,
    occurrences       INTEGER DEFAULT 1,
    recurrence_count  INTEGER DEFAULT 0,
    created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Optional metadata parity with agent_insights
    mdata             TEXT    DEFAULT '{}' CHECK (json_valid(mdata))
);

-- Indices
CREATE INDEX IF NOT EXISTS idx_introspect_file_symbol
    ON introspection_index(filepath, symbol_type);
CREATE INDEX IF NOT EXISTS idx_introspect_relation_type
    ON introspection_index(relation_type);
CREATE INDEX IF NOT EXISTS idx_introspect_ag_tag
    ON introspection_index(ag_tag);
CREATE INDEX IF NOT EXISTS idx_introspect_key_hash
    ON introspection_index(unique_key_hash);

-- Strong dedupe: prefer hash; fallback composite identifier
CREATE UNIQUE INDEX IF NOT EXISTS uq_introspect_key
    ON introspection_index(unique_key_hash);
CREATE UNIQUE INDEX IF NOT EXISTS uq_introspect_natural
    ON introspection_index(filepath, symbol_type, name, lineno);

COMMIT;

