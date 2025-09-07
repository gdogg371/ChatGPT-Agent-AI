-- scripts/migrations/001_create_capabilities.sql
-- Version: 2.0
-- Created: 2025-07-29
-- Description: Schema for persistent agent capabilities registry
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS capabilities (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    confidence REAL CHECK (confidence >= 0.0 AND confidence <= 1.0) DEFAULT 1.0,
    source TEXT NOT NULL DEFAULT 'manual',
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_capabilities_name
    ON capabilities(name);
CREATE INDEX IF NOT EXISTS idx_capabilities_source
    ON capabilities(source);
-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- 
--
-- DROP TRIGGER IF EXISTS trg_update_capabilities_updated_at ON capabilities;
-- 
-- === Row Level Security Policy (Optional Example) ===
-- 
-- 
-- === Example Insert for Testing ===
INSERT INTO capabilities
    (name, description, confidence, source)
VALUES
    ('ag1_patch_lifecycle', 'Tracks patch states, applies and rolls back patches.', 0.95, 'manual');
-- === End Migration ===
COMMIT;
COMMIT;
