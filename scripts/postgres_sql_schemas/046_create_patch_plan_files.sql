-- scripts/migrations/002_create_patch_plan_files.sql
-- Version: 2.0
-- Created: 2025-07-22
-- Description: Normalized table for individual file actions in each patch plan.

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.patch_plan_files (
    id SERIAL PRIMARY KEY,
    patch_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    action VARCHAR(32) NOT NULL DEFAULT 'modify',
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.patch_plan_files IS
    'Stores individual file entries associated with each patch plan. Enables file-level tracking, auditing, and rollback coordination.';

COMMENT ON COLUMN public.patch_plan_files.id IS
    'Primary key (autoincrementing row ID).';
COMMENT ON COLUMN public.patch_plan_files.patch_id IS
    'Patch plan ID (foreign key to patch_plan.id). Defines which patch this file belongs to.';
COMMENT ON COLUMN public.patch_plan_files.file_path IS
    'Absolute or relative path to the file being modified or added.';
COMMENT ON COLUMN public.patch_plan_files.action IS
    'Action to perform: modify, create, delete, or review.';
COMMENT ON COLUMN public.patch_plan_files.metadata IS
    'Optional metadata: hash, trust tags, AG coverage, reason for change.';
COMMENT ON COLUMN public.patch_plan_files.created_at IS
    'Row creation timestamp (immutable).';
COMMENT ON COLUMN public.patch_plan_files.updated_at IS
    'Last-modified timestamp (updated on file reassignment or metadata edit).';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_patch_plan_file_patch
    ON public.patch_plan_files(patch_id);

CREATE INDEX IF NOT EXISTS idx_patch_plan_file_path
    ON public.patch_plan_files(file_path);

-- === Foreign Key Constraint (assumes patch_plan table exists) ===

ALTER TABLE public.patch_plan_files
    ADD CONSTRAINT fk_patch_plan_files_plan
    FOREIGN KEY (patch_id) REFERENCES public.patch_plan(id)
    ON DELETE CASCADE;

-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- CREATE OR REPLACE FUNCTION update_patch_plan_files_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
--
-- DROP TRIGGER IF EXISTS trg_update_patch_plan_files_updated_at ON public.patch_plan_files;
-- CREATE TRIGGER trg_update_patch_plan_files_updated_at
--     BEFORE UPDATE ON public.patch_plan_files
--     FOR EACH ROW EXECUTE FUNCTION update_patch_plan_files_updated_at();

-- === Row Level Security Policy (Optional Example) ===
-- ALTER TABLE public.patch_plan_files ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY patch_file_isolation
--     ON public.patch_plan_files
--     USING (patch_id = current_setting('app.current_patch_id'));

-- === Example Insert for Testing ===

INSERT INTO public.patch_plan_files
    (patch_id, file_path, action, metadata)
VALUES
    ('AG12_mem_fix', 'backend/core/memory.py', 'modify', '{"reason":"fix memory retention edge case"}');

-- === End Migration ===

COMMIT;

COMMIT;
