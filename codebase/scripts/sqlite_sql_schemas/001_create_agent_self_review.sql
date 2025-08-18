-- scripts/migrations/001_create_agent_self_review.sql
-- Version: 2.0
-- Created: 2024-07-06
-- Description: Schema for agent self-review (periodic agent self-assessment/reflective logs)
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS agent_self_review (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL,
    review_cycle INTEGER NOT NULL,
    review_timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    review_text TEXT NOT NULL,
    reviewer TEXT NOT NULL DEFAULT 'self',
    score REAL CHECK (score >= 0.0 AND score <= 1.0),
    status TEXT NOT NULL DEFAULT 'pending',
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
 used for agent-initiated or externally-audited reviews.';
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_self_review_agent_cycle
    ON agent_self_review(agent_id, review_cycle);
CREATE INDEX IF NOT EXISTS idx_self_review_status
    ON agent_self_review(status);
-- === (Optional) Foreign Key Constraint - Uncomment and link if agents table exists ===
-- ALTER TABLE agent_self_review
--     ADD CONSTRAINT fk_agent_self_review_agent
--     FOREIGN KEY (agent_id) REFERENCES agents(id)
--     ON DELETE CASCADE;
-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- 
--
-- DROP TRIGGER IF EXISTS trg_update_self_review_updated_at ON agent_self_review;
-- 
-- === Row Level Security Policy (Optional Example) ===
-- 
-- 
-- === Example Insert for Testing ===
INSERT INTO agent_self_review
    (agent_id, review_cycle, review_text, reviewer, score, status)
VALUES
    (1, 1, 'System initial self-review: all diagnostics passing, no errors detected.', 'self', 0.97, 'complete');
-- === End Migration ===
COMMIT;
COMMIT;
