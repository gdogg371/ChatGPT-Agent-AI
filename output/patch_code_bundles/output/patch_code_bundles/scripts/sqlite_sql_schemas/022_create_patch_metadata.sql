-- scripts/migrations/013_create_patch_metadata.sql
-- Version: 2.0
-- Created: 2025-07-11
-- Description: Schema for storing patch metadata used in simulation, forecasting, and execution planning (AG48+)
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS patch_metadata (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    modifies TEXT,  -- Comma-separated list of modified domains (e.g. "trust,core")
    targets TEXT,   -- Comma-separated affected subsystems (e.g. "cli,core")
    reboot_required INTEGER NOT NULL DEFAULT FALSE,
    trust_level TEXT NOT NULL DEFAULT 'low',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_patch_metadata_trust
    ON patch_metadata(trust_level);
CREATE INDEX IF NOT EXISTS idx_patch_metadata_targets
    ON patch_metadata(targets);
-- === Optional Trigger for updated_at field ===
-- 
--
-- DROP TRIGGER IF EXISTS trg_patch_metadata_updated_at ON patch_metadata;
-- 
-- === Example Insert for Testing ===
INSERT INTO patch_metadata (id, name, modifies, targets, reboot_required, trust_level)
VALUES
    ('AG48', 'Patch Chain Simulator', 'forecast', 'planner,simulation', FALSE, 'low')
ON CONFLICT (id) DO NOTHING;
-- === End Migration ===
COMMIT;
COMMIT;
