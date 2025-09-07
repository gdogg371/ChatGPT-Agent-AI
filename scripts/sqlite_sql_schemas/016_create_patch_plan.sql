-- scripts/migrations/016_patch_plan.sql
-- Version: 3.0
-- Updated: 2025-07-27
-- Description: Redesigned patch_plan schema to unify canonical plan storage, metadata, validation state, and AG mapping
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS patch_plan (
    id INTEGER PRIMARY KEY,
    -- Foreign keys
    goal_id INTEGER NOT NULL,
    -- Core metadata
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    version TEXT DEFAULT '1.0',
    ag_reference TEXT[] NOT NULL DEFAULT '{}',
    -- Planning logic
    llm_model TEXT NOT NULL DEFAULT 'gpt-4',
    planned_by TEXT NOT NULL DEFAULT 'agent',
    plan_timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- Lifecycle status
    status TEXT NOT NULL DEFAULT 'planned',
    validated INTEGER NOT NULL DEFAULT FALSE,
    -- Plan content
    plan_checksum TEXT NOT NULL,
    actions_json TEXT NOT NULL,
    -- Optional annotations
    plan_notes TEXT,
    metadata TEXT DEFAULT '{}',
    -- Timestamps
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_patch_plan_goal
    ON patch_plan(goal_id);
CREATE INDEX IF NOT EXISTS idx_patch_plan_status
    ON patch_plan(status);
CREATE INDEX IF NOT EXISTS idx_patch_plan_validated
    ON patch_plan(validated);
CREATE INDEX IF NOT EXISTS idx_patch_plan_ag
    ON patch_plan USING GIN (ag_reference);
CREATE INDEX IF NOT EXISTS idx_patch_plan_checksum
    ON patch_plan(plan_checksum);
-- === End Migration ===
COMMIT;
