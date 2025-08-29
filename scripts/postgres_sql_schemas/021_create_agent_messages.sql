-- scripts/migrations/043_create_agent_messages.sql
-- Version: 2.0
-- Created: 2025-07-11
-- Description: Schema for agent-to-agent messaging (durable message bus for inter-agent communication)

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.agent_messages (
    id SERIAL PRIMARY KEY,
    sender_id INTEGER NOT NULL,
    receiver_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    msg_type VARCHAR(64) NOT NULL DEFAULT 'info',
    metadata JSONB DEFAULT '{}'::jsonb,
    read BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.agent_messages IS
    'Durable inter-agent messaging system. Each row is a message sent between agents with metadata, type, and read tracking.';

COMMENT ON COLUMN public.agent_messages.id IS
    'Primary key (autoincrementing message ID).';
COMMENT ON COLUMN public.agent_messages.sender_id IS
    'Sender agent ID (should FK to agents table).';
COMMENT ON COLUMN public.agent_messages.receiver_id IS
    'Receiver agent ID (should FK to agents table).';
COMMENT ON COLUMN public.agent_messages.content IS
    'Message body or command content (human-readable or structured text).';
COMMENT ON COLUMN public.agent_messages.msg_type IS
    'Optional classification of message: info, task, warning, sync, etc.';
COMMENT ON COLUMN public.agent_messages.metadata IS
    'Flexible structured JSON metadata attached to the message.';
COMMENT ON COLUMN public.agent_messages.read IS
    'Whether the receiver has read and processed the message.';
COMMENT ON COLUMN public.agent_messages.created_at IS
    'When the message was first sent.';
COMMENT ON COLUMN public.agent_messages.updated_at IS
    'Last-modified timestamp (e.g., on read or edit).';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_agent_messages_receiver_read
    ON public.agent_messages(receiver_id, read);

CREATE INDEX IF NOT EXISTS idx_agent_messages_created
    ON public.agent_messages(created_at);

-- === (Optional) Foreign Key Constraints - Uncomment if agents table exists ===
-- ALTER TABLE public.agent_messages
--     ADD CONSTRAINT fk_messages_sender
--     FOREIGN KEY (sender_id) REFERENCES public.agents(id)
--     ON DELETE CASCADE;

-- ALTER TABLE public.agent_messages
--     ADD CONSTRAINT fk_messages_receiver
--     FOREIGN KEY (receiver_id) REFERENCES public.agents(id)
--     ON DELETE CASCADE;

-- === Audit Trigger for updated_at (Optional) ===
-- CREATE OR REPLACE FUNCTION update_agent_messages_updated_at()
-- RETURNS TRIGGER AS $$
-- BEGIN
--     NEW.updated_at = NOW();
--     RETURN NEW;
-- END;
-- $$ LANGUAGE plpgsql;
-- 
-- DROP TRIGGER IF EXISTS trg_update_messages_updated_at ON public.agent_messages;
-- CREATE TRIGGER trg_update_messages_updated_at
--     BEFORE UPDATE ON public.agent_messages
--     FOR EACH ROW EXECUTE FUNCTION update_agent_messages_updated_at();

-- === Row Level Security Policy (Optional Example) ===
-- ALTER TABLE public.agent_messages ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY agent_messages_isolation
--     ON public.agent_messages
--     USING (
--         sender_id = current_setting('app.current_agent_id')::integer OR
--         receiver_id = current_setting('app.current_agent_id')::integer
--     );

-- === Example Insert for Testing ===

INSERT INTO public.agent_messages
    (sender_id, receiver_id, content, msg_type, metadata)
VALUES
    (1, 2, 'Requesting goal sync for upcoming cycle.', 'sync', '{"urgency": "low"}'::jsonb);

-- === End Migration ===

COMMIT;

COMMIT;
