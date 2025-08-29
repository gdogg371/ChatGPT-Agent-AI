-- scripts/migrations/XXX_create_patch_trust_assessments.sql
-- Version: 1.0
-- Created: 2025-07-19
-- Description: Schema for storing trust evaluations of patches (AG39 trust assessments)
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS patch_trust_assessments (
    id INTEGER PRIMARY KEY,
    patch_id TEXT NOT NULL,
    ag_reference TEXT[] NOT NULL,
    trust_score REAL CHECK (trust_score >= 0.0 AND trust_score <= 1.0),
    trusted INTEGER NOT NULL DEFAULT FALSE,
    reason TEXT NOT NULL,
    tested_by TEXT NOT NULL DEFAULT 'self',
    tested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_patch_trust_patch_id
    ON patch_trust_assessments(patch_id);
CREATE INDEX IF NOT EXISTS idx_patch_trust_score
    ON patch_trust_assessments(trust_score DESC);
-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- 
-- DROP TRIGGER IF EXISTS trg_patch_trust_updated_at ON patch_trust_assessments;
-- 
-- === Example Insert for Testing ===
INSERT INTO patch_trust_assessments
    (patch_id, ag_reference, trust_score, trusted, reason, tested_by)
VALUES
    ('patch_001', ARRAY['AG-3', 'AG-12'], 0.91, TRUE, 'Score 0.91 based on AG match with AG-3 and AG-12', 'self');
-- === End Migration ===
COMMIT;
COMMIT;
