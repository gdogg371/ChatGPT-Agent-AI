-- scripts/migrations/002_create_patch_feedback.sql
-- Version: 2.0
-- Created: 2025-07-19
-- Description: Schema for patch feedback and LLM reflective feedback loop results
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS patch_feedback (
    id INTEGER PRIMARY KEY,
    patch_id TEXT NOT NULL,
    feedback_text TEXT NOT NULL,
    feedback_type TEXT NOT NULL DEFAULT 'llm',
    source TEXT NOT NULL DEFAULT 'agent',
    model_used TEXT DEFAULT NULL,
    prompt_used TEXT DEFAULT NULL,
    status TEXT NOT NULL DEFAULT 'complete',
    score REAL CHECK (score >= 0.0 AND score <= 1.0),
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_patch_feedback_patch_id
    ON patch_feedback(patch_id);
CREATE INDEX IF NOT EXISTS idx_patch_feedback_status
    ON patch_feedback(status);
CREATE INDEX IF NOT EXISTS idx_patch_feedback_type
    ON patch_feedback(feedback_type);
-- === Optional: Trigger to auto-update updated_at ===
-- 
--
-- DROP TRIGGER IF EXISTS trg_update_patch_feedback_updated_at ON patch_feedback;
-- 
-- === Example Insert for Testing ===
INSERT INTO patch_feedback
    (patch_id, feedback_text, feedback_type, source, model_used, score)
VALUES
    ('patch_001', 'Patch applied successfully. All test cases passed.', 'llm', 'agent', 'gpt-4', 0.98);
-- === End Migration ===
COMMIT;
COMMIT;
