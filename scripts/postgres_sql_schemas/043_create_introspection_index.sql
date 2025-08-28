-- scripts/migrations/002_create_introspection_index.sql
-- Version: 2.0
-- Created: 2025-08-03
-- Description: Schema for introspection_index (symbol registry, code lineage, and lifecycle tracking)

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.introspection_index (
    id SERIAL PRIMARY KEY,
    filepath TEXT NOT NULL,
    symbol_type VARCHAR(64) NOT NULL DEFAULT 'unknown', -- e.g. function, class, route, summary
    name TEXT,
    lineno INTEGER DEFAULT 0,
    route_method VARCHAR(16),
    route_path TEXT,
    ag_tag VARCHAR(32),
    description TEXT,
    target_symbol TEXT,          -- for relations (e.g. calls, imports)
    relation_type VARCHAR(32),   -- e.g. 'calls', 'imports', 'inherits'

    -- === New Lifecycle Columns ===
    unique_key_hash TEXT,                 -- Deterministic ID hash for deduping and updates
    status VARCHAR(32) DEFAULT 'active',  -- active | deprecated | removed
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP,
    occurrences INTEGER DEFAULT 1,
    recurrence_count INTEGER DEFAULT 0,

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.introspection_index IS
    'Tracks discovered code symbols, summaries, and relationships with lifecycle info and AG traceability.';

COMMENT ON COLUMN public.introspection_index.id IS
    'Primary key (autoincremented introspection record ID).';
COMMENT ON COLUMN public.introspection_index.filepath IS
    'Source file path where the symbol was found.';
COMMENT ON COLUMN public.introspection_index.symbol_type IS
    'Type of the symbol: function, class, route, summary, import, call, etc.';
COMMENT ON COLUMN public.introspection_index.name IS
    'Name of the symbol (function/class/route/etc.).';
COMMENT ON COLUMN public.introspection_index.lineno IS
    'Line number where the symbol is defined.';
COMMENT ON COLUMN public.introspection_index.route_method IS
    'HTTP method (if applicable to routes), e.g., GET, POST.';
COMMENT ON COLUMN public.introspection_index.route_path IS
    'HTTP path (if applicable to routes).';
COMMENT ON COLUMN public.introspection_index.ag_tag IS
    'AG identifier that this symbol contributes to or was generated under.';
COMMENT ON COLUMN public.introspection_index.description IS
    'Optional description, summary, or docstring extracted for the symbol.';
COMMENT ON COLUMN public.introspection_index.target_symbol IS
    'If a relationship row: name of the target symbol (e.g., function called).';
COMMENT ON COLUMN public.introspection_index.relation_type IS
    'Type of symbol relationship, such as calls, imports, uses.';
COMMENT ON COLUMN public.introspection_index.unique_key_hash IS
    'Deterministic hash of (filepath + name + symbol_type) used for tracking.';
COMMENT ON COLUMN public.introspection_index.status IS
    'Current status of the symbol: active, removed, deprecated.';
COMMENT ON COLUMN public.introspection_index.discovered_at IS
    'When this symbol was first seen.';
COMMENT ON COLUMN public.introspection_index.last_seen_at IS
    'When it was last confirmed to still exist.';
COMMENT ON COLUMN public.introspection_index.resolved_at IS
    'If removed/resolved, when this occurred.';
COMMENT ON COLUMN public.introspection_index.occurrences IS
    'How many times this symbol has been re-encountered.';
COMMENT ON COLUMN public.introspection_index.recurrence_count IS
    'Number of times a removed symbol later reappeared.';
COMMENT ON COLUMN public.introspection_index.created_at IS
    'Timestamp of record insertion.';
COMMENT ON COLUMN public.introspection_index.updated_at IS
    'Last update timestamp.';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_introspect_file_symbol
    ON public.introspection_index(filepath, symbol_type);

CREATE INDEX IF NOT EXISTS idx_introspect_relation_type
    ON public.introspection_index(relation_type);

CREATE INDEX IF NOT EXISTS idx_introspect_ag_tag
    ON public.introspection_index(ag_tag);

CREATE INDEX IF NOT EXISTS idx_introspect_key_hash
    ON public.introspection_index(unique_key_hash);

-- === Audit Trigger for updated_at (Optional, requires function) ===
-- CREATE OR REPLACE FUNCTION update_introspect_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
--
-- DROP TRIGGER IF EXISTS trg_update_introspect_updated_at ON public.introspection_index;
-- CREATE TRIGGER trg_update_introspect_updated_at
--     BEFORE UPDATE ON public.introspection_index
--     FOR EACH ROW EXECUTE FUNCTION update_introspect_updated_at();

-- === Example Insert for Testing ===

INSERT INTO public.introspection_index
    (filepath, symbol_type, name, lineno, route_method, route_path, ag_tag, description, unique_key_hash)
VALUES
    ('backend/routes/chat.py', 'function', 'chat_handler', 42, 'POST', '/chat', 'AG-1', 'Main chat route handler.', 'hash_chat_handler_v1');

-- === End Migration ===

COMMIT;
COMMIT;
