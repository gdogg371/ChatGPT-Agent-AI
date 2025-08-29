-- scripts/migrations/002_create_patch_test_results.sql
-- Version: 2.0
-- Created: 2025-07-19
-- Description: Schema for patch test result logs (syntax checks + script test outcomes)

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.patch_test_results (
    id SERIAL PRIMARY KEY,
    patch_id TEXT NOT NULL,
    file_path TEXT,
    syntax_ok BOOLEAN NOT NULL DEFAULT TRUE,
    script_tests_ok BOOLEAN NOT NULL DEFAULT TRUE,
    error_msg TEXT,
    script_log TEXT,
    tested_by VARCHAR(128) NOT NULL DEFAULT 'system',
    tested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.patch_test_results IS
    'Stores results of patch syntax validation and test execution. One row per file or patch test.';

COMMENT ON COLUMN public.patch_test_results.id IS
    'Primary key (autoincrementing record ID).';
COMMENT ON COLUMN public.patch_test_results.patch_id IS
    'Unique identifier for the patch this test applies to.';
COMMENT ON COLUMN public.patch_test_results.file_path IS
    'Relative path to the file under test (if applicable).';
COMMENT ON COLUMN public.patch_test_results.syntax_ok IS
    'Indicates whether Python syntax check passed.';
COMMENT ON COLUMN public.patch_test_results.script_tests_ok IS
    'Indicates whether external/script tests passed.';
COMMENT ON COLUMN public.patch_test_results.error_msg IS
    'Any error message from syntax validation.';
COMMENT ON COLUMN public.patch_test_results.script_log IS
    'Standard output or logs from the script test runner.';
COMMENT ON COLUMN public.patch_test_results.tested_by IS
    'Who/what ran the test (e.g., "system", "cli", or agent label).';
COMMENT ON COLUMN public.patch_test_results.tested_at IS
    'When the test was executed.';
COMMENT ON COLUMN public.patch_test_results.metadata IS
    'Flexible JSONB for patch version, runner context, or agent state.';
COMMENT ON COLUMN public.patch_test_results.created_at IS
    'Row creation timestamp.';
COMMENT ON COLUMN public.patch_test_results.updated_at IS
    'Last-modified timestamp (auto-updated on write).';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_patch_test_results_patch_id
    ON public.patch_test_results(patch_id);

CREATE INDEX IF NOT EXISTS idx_patch_test_results_status
    ON public.patch_test_results(syntax_ok, script_tests_ok);

-- === Audit Trigger for updated_at (Optional, requires a function) ===

-- CREATE OR REPLACE FUNCTION update_patch_test_results_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
--
-- DROP TRIGGER IF EXISTS trg_update_patch_test_results_updated_at ON public.patch_test_results;
-- CREATE TRIGGER trg_update_patch_test_results_updated_at
--     BEFORE UPDATE ON public.patch_test_results
--     FOR EACH ROW EXECUTE FUNCTION update_patch_test_results_updated_at();

-- === Example Insert for Testing ===

INSERT INTO public.patch_test_results
    (patch_id, file_path, syntax_ok, script_tests_ok, error_msg, script_log)
VALUES
    ('test_patch_xyz', 'backend/utils/hello.py', TRUE, TRUE, NULL, 'All checks passed successfully.');

-- === End Migration ===

COMMIT;

COMMIT;
