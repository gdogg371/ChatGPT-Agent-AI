-- scripts/migrations/002_create_patch_risk_evaluations.sql
-- Version: 2.0
-- Created: 2025-07-19
-- Description: Schema for patch risk evaluations (risk score, HIL, reboot, auto-apply safety)
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS patch_risk_evaluations (
    id INTEGER PRIMARY KEY,
    patch_id TEXT NOT NULL,
    evaluated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    risk_score REAL CHECK (risk_score >= 0.0 AND risk_score <= 1.0),
    human_required INTEGER NOT NULL DEFAULT FALSE,
    reboot_required INTEGER NOT NULL DEFAULT FALSE,
    is_safe_to_autoapply INTEGER NOT NULL DEFAULT FALSE,
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_risk_eval_patch_time
    ON patch_risk_evaluations(patch_id, evaluated_at DESC);
CREATE INDEX IF NOT EXISTS idx_risk_eval_autoapply
    ON patch_risk_evaluations(is_safe_to_autoapply);
-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- 
--
-- DROP TRIGGER IF EXISTS trg_update_patch_risk_eval_updated_at ON patch_risk_evaluations;
-- 
-- === Row Level Security Policy (Optional Example) ===
-- 
-- 
-- === Example Insert for Testing ===
INSERT INTO patch_risk_evaluations
    (patch_id, risk_score, human_required, reboot_required, is_safe_to_autoapply)
VALUES
    ('hotfix-secure-logrotate', 0.18, FALSE, FALSE, TRUE);
-- === End Migration ===
COMMIT;
COMMIT;
