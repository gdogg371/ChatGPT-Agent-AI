-- scripts/migrations/002_create_agent_registry.sql
-- Version: 2.0
-- Created: 2024-07-06
-- Description: Schema for registered agent definitions, identity, and configuration

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.agent_registry (
    id SERIAL PRIMARY KEY,
    agent_name VARCHAR(128) NOT NULL UNIQUE,
    public_key TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    role VARCHAR(64) NOT NULL DEFAULT 'autonomous_agent',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    config JSONB DEFAULT '{}'::jsonb,
    description TEXT,
    owner VARCHAR(128),
    last_seen TIMESTAMP,
    is_trusted BOOLEAN NOT NULL DEFAULT FALSE
);

-- === Comments for Documentation and Traceability ===

COMMENT ON TABLE public.agent_registry IS
    'Master table for all agents registered in the system. Tracks agent identity, keys, status, config, trust, and ownership metadata.';

COMMENT ON COLUMN public.agent_registry.id IS
    'Primary key. Unique agent registry identifier.';
COMMENT ON COLUMN public.agent_registry.agent_name IS
    'Globally unique, human-friendly name for the agent (required, unique).';
COMMENT ON COLUMN public.agent_registry.public_key IS
    'Agent''s cryptographic public key for identity, trust, and signing.';
COMMENT ON COLUMN public.agent_registry.status IS
    'Operational status of the agent (e.g., active, suspended, revoked, offline).';
COMMENT ON COLUMN public.agent_registry.role IS
    'Agent type or operating role (e.g., "autonomous_agent", "observer", etc).';
COMMENT ON COLUMN public.agent_registry.created_at IS
    'Timestamp when this agent record was created.';
COMMENT ON COLUMN public.agent_registry.updated_at IS
    'Timestamp of last registry update (should be set by update trigger).';
COMMENT ON COLUMN public.agent_registry.config IS
    'Agent-specific configuration in structured JSONB format.';
COMMENT ON COLUMN public.agent_registry.description IS
    'Optional description, notes, or human-friendly info about the agent.';
COMMENT ON COLUMN public.agent_registry.owner IS
    'Logical or physical owner of the agent (email, org, username, etc).';
COMMENT ON COLUMN public.agent_registry.last_seen IS
    'Timestamp of last confirmed activity, heartbeat, or ping.';
COMMENT ON COLUMN public.agent_registry.is_trusted IS
    'Flag indicating whether this agent is explicitly trusted by system policies.';

-- === Indexes for Performance ===

CREATE INDEX IF NOT EXISTS idx_agent_registry_status
    ON public.agent_registry(status);

CREATE INDEX IF NOT EXISTS idx_agent_registry_owner
    ON public.agent_registry(owner);

-- === (Optional) Foreign Key Examples (uncomment and adapt as needed) ===
-- ALTER TABLE public.agent_registry
--     ADD CONSTRAINT fk_agent_registry_owner
--     FOREIGN KEY (owner) REFERENCES public.users(username)
--     ON DELETE SET NULL;

-- === Audit Trigger for updated_at (Optional, requires a function) ===
-- CREATE OR REPLACE FUNCTION update_agent_registry_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
--
-- DROP TRIGGER IF EXISTS trg_update_agent_registry_updated_at ON public.agent_registry;
-- CREATE TRIGGER trg_update_agent_registry_updated_at
--     BEFORE UPDATE ON public.agent_registry
--     FOR EACH ROW EXECUTE FUNCTION update_agent_registry_updated_at();

-- === Row-Level Security Policy Example (Optional) ===
-- ALTER TABLE public.agent_registry ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY agent_registry_owner_access
--     ON public.agent_registry
--     USING (owner = current_user OR is_trusted);

-- === Example Insert for Testing ===

INSERT INTO public.agent_registry
    (agent_name, public_key, status, role, config, description, owner, is_trusted)
VALUES
    ('agent_core_1', '-----BEGIN PUBLIC KEY-----MIIBIjANBgkqh...IDAQAB-----END PUBLIC KEY-----', 'active', 'autonomous_agent', '{"learning":true,"max_goals":5}', 'Main core agent, responsible for orchestration', 'admin', TRUE);

-- === End Migration ===

COMMIT;

COMMIT;
COMMIT;
