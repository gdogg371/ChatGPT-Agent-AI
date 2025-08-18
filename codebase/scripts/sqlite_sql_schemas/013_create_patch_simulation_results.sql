-- scripts/migrations/013_patch_simulation_results.sql
-- Version: 2.0
-- Created: 2024-07-10
-- Description: Schema for storing patch simulation results (diffs, AST notes, predicted side-effects)
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS patch_simulation_results (
    id INTEGER PRIMARY KEY,
    patch_id INTEGER NOT NULL,
    diff_preview TEXT NOT NULL,
    ast_notes TEXT,
    predicted_side_effects TEXT,
    confidence_score REAL CHECK (confidence_score >= 0.0 AND confidence_score <= 1.0),
    timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_sim_results_patch
    ON patch_simulation_results(patch_id);
-- === (Optional) Foreign Key Constraint - Uncomment if patch_history exists ===
-- ALTER TABLE patch_simulation_results
--     ADD CONSTRAINT fk_sim_results_patch
--     FOREIGN KEY (patch_id) REFERENCES patch_history(id)
--     ON DELETE CASCADE;
-- === Example Insert for Testing ===
INSERT INTO patch_simulation_results
    (patch_id, diff_preview, ast_notes, predicted_side_effects, confidence_score)
VALUES
    (1, '--- OLD\n+++ NEW\n+ def foo(): pass', 'Unused: foo; TODOs: []', 'Writes to log file', 0.94);
-- === End Migration ===
COMMIT;
