-- scripts/migrations/002_create_capability_dependencies.sql
-- Version: 2.0
-- Created: 2025-07-29
-- Description: Capability dependency graph (edges between capabilities)
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS capability_dependencies (
    id INTEGER PRIMARY KEY,
    capability_name TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    relation_type TEXT NOT NULL DEFAULT 'hard',
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_dep_capability_name
    ON capability_dependencies(capability_name);
CREATE INDEX IF NOT EXISTS idx_dep_depends_on
    ON capability_dependencies(depends_on);
-- === Foreign Key Hints (Optional, if capabilities table FK needed) ===
-- ALTER TABLE capability_dependencies
--     ADD CONSTRAINT fk_dep_capability
--     FOREIGN KEY (capability_name) REFERENCES capabilities(name)
--     ON DELETE CASCADE;
--
-- ALTER TABLE capability_dependencies
--     ADD CONSTRAINT fk_dep_depends_on
--     FOREIGN KEY (depends_on) REFERENCES capabilities(name)
--     ON DELETE CASCADE;
-- === Audit Trigger for updated_at (Optional) ===
-- 
--
-- DROP TRIGGER IF EXISTS trg_update_capability_dependencies_updated_at ON capability_dependencies;
-- 
-- === Example Insert for Testing ===
INSERT INTO capability_dependencies
    (capability_name, depends_on, relation_type)
VALUES
    ('ag9_patch_planner', 'ag1_patch_lifecycle', 'hard');
-- === End Migration ===
COMMIT;
COMMIT;
