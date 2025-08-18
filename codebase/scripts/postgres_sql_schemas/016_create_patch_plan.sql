-- scripts/migrations/016_patch_plan.sql
-- Version: 3.0
-- Updated: 2025-07-27
-- Description: Redesigned patch_plan schema to unify canonical plan storage, metadata, validation state, and AG mapping

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.patch_plan (
    id SERIAL PRIMARY KEY,

    -- Foreign keys
    goal_id INTEGER NOT NULL,

    -- Core metadata
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    version VARCHAR(32) DEFAULT '1.0',
    ag_reference TEXT[] NOT NULL DEFAULT '{}',

    -- Planning logic
    llm_model VARCHAR(128) NOT NULL DEFAULT 'gpt-4',
    planned_by VARCHAR(64) NOT NULL DEFAULT 'agent',
    plan_timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Lifecycle status
    status VARCHAR(64) NOT NULL DEFAULT 'planned',
    validated BOOLEAN NOT NULL DEFAULT FALSE,

    -- Plan content
    plan_checksum TEXT NOT NULL,
    actions_json JSONB NOT NULL,

    -- Optional annotations
    plan_notes TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,

    -- Timestamps
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Traceability ===

COMMENT ON TABLE public.patch_plan IS
    'Canonical, DB-backed patch plan store. Holds full plan metadata, actions, AG links, and LLM trace.';

COMMENT ON COLUMN public.patch_plan.id IS
    'Primary key for each patch plan.';

COMMENT ON COLUMN public.patch_plan.goal_id IS
    'References the agent goal driving this patch.';

COMMENT ON COLUMN public.patch_plan.name IS
    'Human-readable title of the patch (e.g., "Enable AG-13 introspection").';

COMMENT ON COLUMN public.patch_plan.description IS
    'Short description of the patch intent and scope.';

COMMENT ON COLUMN public.patch_plan.version IS
    'Version number for patch schema compatibility and history.';

COMMENT ON COLUMN public.patch_plan.ag_reference IS
    'List of AG capabilities (e.g. AG-10, AG-13) this patch addresses. Used for traceability and audit.';

COMMENT ON COLUMN public.patch_plan.llm_model IS
    'LLM used to generate the patch plan (e.g. gpt-4, claude, mistral).';

COMMENT ON COLUMN public.patch_plan.planned_by IS
    'Entity or subsystem that generated this plan (e.g. agent, user, daemon).';

COMMENT ON COLUMN public.patch_plan.plan_timestamp IS
    'When the plan was originally created.';

COMMENT ON COLUMN public.patch_plan.status IS
    'Current lifecycle status: planned, validated, rejected, applied, rolled_back.';

COMMENT ON COLUMN public.patch_plan.validated IS
    'Boolean flag indicating whether the patch has passed schema validation.';

COMMENT ON COLUMN public.patch_plan.plan_checksum IS
    'Hash of the plan content for drift detection and trust assessments.';

COMMENT ON COLUMN public.patch_plan.actions_json IS
    'Serialized JSON structure of the patch plan’s actions (AG-8/10/14/etc).';

COMMENT ON COLUMN public.patch_plan.plan_notes IS
    'Optional planning notes, AG reasoning trace, or LLM commentary.';

COMMENT ON COLUMN public.patch_plan.metadata IS
    'Flexible metadata field (execution flags, confidence scores, planning origin).';

COMMENT ON COLUMN public.patch_plan.created_at IS
    'Record creation timestamp. Immutable.';

COMMENT ON COLUMN public.patch_plan.updated_at IS
    'Last modified timestamp. Use with triggers for audit support.';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_patch_plan_goal
    ON public.patch_plan(goal_id);

CREATE INDEX IF NOT EXISTS idx_patch_plan_status
    ON public.patch_plan(status);

CREATE INDEX IF NOT EXISTS idx_patch_plan_validated
    ON public.patch_plan(validated);

CREATE INDEX IF NOT EXISTS idx_patch_plan_ag
    ON public.patch_plan USING GIN (ag_reference);

CREATE INDEX IF NOT EXISTS idx_patch_plan_checksum
    ON public.patch_plan(plan_checksum);

-- === End Migration ===

COMMIT;
