-- scripts/migrations/014_patch_rejections.sql
-- Version: 2.0
-- Created: 2024-07-10
-- Description: Schema for logging patch rejections with reason, source, and timestamp
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS patch_rejections (
    id INTEGER PRIMARY KEY,
    patch_id INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    details TEXT,
    rejected_by TEXT NOT NULL DEFAULT 'agent',
    timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_patch_rejections_patch
    ON patch_rejections(patch_id);
-- === (Optional) Foreign Key Constraint - Uncomment if patch_history exists ===
-- ALTER TABLE patch_rejections
--     ADD CONSTRAINT fk_patch_rejections_patch
--     FOREIGN KEY (patch_id) REFERENCES patch_history(id)
--     ON DELETE CASCADE;
-- === Example Insert for Testing ===
INSERT INTO patch_rejections
    (patch_id, reason_code, details, rejected_by)
VALUES
    (1, 'HUMAN_REJECTED', 'User declined patch via CLI approval process.', 'human');
-- === End Migration ===
COMMIT;
