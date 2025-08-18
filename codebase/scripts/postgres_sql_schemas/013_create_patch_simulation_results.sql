-- scripts/migrations/013_patch_simulation_results.sql
-- Version: 2.0
-- Created: 2024-07-10
-- Description: Schema for storing patch simulation results (diffs, AST notes, predicted side-effects)

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.patch_simulation_results (
    id SERIAL PRIMARY KEY,
    patch_id INTEGER NOT NULL,
    diff_preview TEXT NOT NULL,
    ast_notes TEXT,
    predicted_side_effects TEXT,
    confidence_score DOUBLE PRECISION CHECK (confidence_score >= 0.0 AND confidence_score <= 1.0),
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.patch_simulation_results IS
    'Stores simulation outcomes for each patch, including code diffs, AST analysis, side-effect predictions, and confidence scores. Supports AG45a simulation and planning audits.';

COMMENT ON COLUMN public.patch_simulation_results.id IS
    'Primary key for each simulation result.';
COMMENT ON COLUMN public.patch_simulation_results.patch_id IS
    'Foreign key to patch_history (one-to-one expected).';
COMMENT ON COLUMN public.patch_simulation_results.diff_preview IS
    'Unified diff-style preview of patch changes.';
COMMENT ON COLUMN public.patch_simulation_results.ast_notes IS
    'Static analysis notes (e.g. unused funcs, TODOs).';
COMMENT ON COLUMN public.patch_simulation_results.predicted_side_effects IS
    'High-level summary of runtime or external effects the patch might trigger.';
COMMENT ON COLUMN public.patch_simulation_results.confidence_score IS
    'Optional LLM or simulation confidence score (0.0 to 1.0).';
COMMENT ON COLUMN public.patch_simulation_results.timestamp IS
    'Timestamp when simulation was performed.';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_sim_results_patch
    ON public.patch_simulation_results(patch_id);

-- === (Optional) Foreign Key Constraint - Uncomment if patch_history exists ===
-- ALTER TABLE public.patch_simulation_results
--     ADD CONSTRAINT fk_sim_results_patch
--     FOREIGN KEY (patch_id) REFERENCES public.patch_history(id)
--     ON DELETE CASCADE;

-- === Example Insert for Testing ===

INSERT INTO public.patch_simulation_results
    (patch_id, diff_preview, ast_notes, predicted_side_effects, confidence_score)
VALUES
    (1, '--- OLD\n+++ NEW\n+ def foo(): pass', 'Unused: foo; TODOs: []', 'Writes to log file', 0.94);

-- === End Migration ===

COMMIT;
