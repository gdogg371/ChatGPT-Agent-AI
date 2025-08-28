-- scripts/migrations/002_create_patch_file_versions.sql
-- Version: 2.0
-- Created: 2025-07-27
-- Description: Schema for patch file version snapshots (versioned file content for diff, rollback, diagnostics)

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.patch_file_versions (
    id SERIAL PRIMARY KEY,
    patch_id INTEGER,
    path TEXT NOT NULL,
    version INTEGER NOT NULL,
    snapshot TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.patch_file_versions IS
    'Stores versioned snapshots of file contents as used in patch planning, diffing, and rollback. Each row records a unique version of a file at a point in time.';

COMMENT ON COLUMN public.patch_file_versions.id IS
    'Primary key (autoincrementing snapshot version record ID).';
COMMENT ON COLUMN public.patch_file_versions.patch_id IS
    'Optional foreign key reference to patch request/plan (if applicable).';
COMMENT ON COLUMN public.patch_file_versions.path IS
    'Logical or physical file path this snapshot refers to.';
COMMENT ON COLUMN public.patch_file_versions.version IS
    'Monotonically increasing version per file path.';
COMMENT ON COLUMN public.patch_file_versions.snapshot IS
    'Full textual snapshot of the file at that version.';
COMMENT ON COLUMN public.patch_file_versions.created_at IS
    'Timestamp of when this snapshot was created or recorded.';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_patch_file_path_version
    ON public.patch_file_versions(path, version DESC);

CREATE INDEX IF NOT EXISTS idx_patch_file_patch_id
    ON public.patch_file_versions(patch_id);

-- === (Optional) Foreign Key Constraint - Uncomment if patches table exists ===
-- ALTER TABLE public.patch_file_versions
--     ADD CONSTRAINT fk_patch_file_versions_patch
--     FOREIGN KEY (patch_id) REFERENCES public.patch_requests(id)
--     ON DELETE CASCADE;

-- === Example Insert for Testing ===

INSERT INTO public.patch_file_versions
    (patch_id, path, version, snapshot)
VALUES
    (NULL, 'backend/core/memory/memory.py', 1, 'def get_all_memory_items():\n    return []');

-- === End Migration ===

COMMIT;
