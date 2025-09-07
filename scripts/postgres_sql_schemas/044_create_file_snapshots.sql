-- scripts/migrations/002_create_file_snapshots.sql
-- Version: 2.0
-- Created: 2025-07-20
-- Description: Schema for pre/post file state snapshots used in patch integrity verification

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.file_snapshots (
    id SERIAL PRIMARY KEY,
    snapshot_id UUID NOT NULL,
    agent_id INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    snapshot_type VARCHAR(32) NOT NULL CHECK (snapshot_type IN ('pre', 'post')),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.file_snapshots IS
    'Stores file hash snapshots for patch verification. Used to compare pre- and post-patch file states.';

COMMENT ON COLUMN public.file_snapshots.id IS
    'Primary key for each snapshot record.';
COMMENT ON COLUMN public.file_snapshots.snapshot_id IS
    'UUID grouping for all snapshot entries within a single patch event.';
COMMENT ON COLUMN public.file_snapshots.agent_id IS
    'ID of the agent that recorded the snapshot.';
COMMENT ON COLUMN public.file_snapshots.file_path IS
    'Path to the file being snapshotted.';
COMMENT ON COLUMN public.file_snapshots.file_hash IS
    'Content hash (e.g., SHA256) of the file at the snapshot moment.';
COMMENT ON COLUMN public.file_snapshots.snapshot_type IS
    'Whether this is a pre-patch or post-patch snapshot.';
COMMENT ON COLUMN public.file_snapshots.created_at IS
    'When the snapshot record was created.';
COMMENT ON COLUMN public.file_snapshots.updated_at IS
    'When the snapshot record was last updated.';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_snapshot_id
    ON public.file_snapshots(snapshot_id);

CREATE INDEX IF NOT EXISTS idx_file_path
    ON public.file_snapshots(file_path);

CREATE INDEX IF NOT EXISTS idx_snapshot_type_path
    ON public.file_snapshots(snapshot_type, file_path);

-- === Foreign Key Constraint (Optional) ===
-- Uncomment if `agents` table exists

-- ALTER TABLE public.file_snapshots
--     ADD CONSTRAINT fk_file_snapshots_agent
--     FOREIGN KEY (agent_id) REFERENCES public.agents(id)
--     ON DELETE CASCADE;

-- === Audit Trigger for updated_at (Optional) ===

-- CREATE OR REPLACE FUNCTION update_file_snapshots_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;

-- DROP TRIGGER IF EXISTS trg_update_file_snapshots_updated_at ON public.file_snapshots;

-- CREATE TRIGGER trg_update_file_snapshots_updated_at
--     BEFORE UPDATE ON public.file_snapshots
--     FOR EACH ROW EXECUTE FUNCTION update_file_snapshots_updated_at();

-- === Row Level Security Policy (Optional) ===
-- ALTER TABLE public.file_snapshots ENABLE ROW LEVEL SECURITY;

-- CREATE POLICY file_snapshot_agent_isolation
--     ON public.file_snapshots
--     USING (agent_id = current_setting('app.current_agent_id')::integer);

-- === Example Insert for Testing ===

INSERT INTO public.file_snapshots (
    snapshot_id, agent_id, file_path, file_hash, snapshot_type
)
VALUES (
    gen_random_uuid(), 1, '/app/main.py', 'abc123def456', 'pre'
);

-- === End Migration ===

COMMIT;

COMMIT;
