-- scripts/migrations/043_create_agent_messages.sql
-- Version: 2.0
-- Created: 2025-07-11
-- Description: Schema for agent-to-agent messaging (durable message bus for inter-agent communication)
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY,
    sender_id INTEGER NOT NULL,
    receiver_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    msg_type TEXT NOT NULL DEFAULT 'info',
    metadata TEXT DEFAULT '{}',
    read INTEGER NOT NULL DEFAULT FALSE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_agent_messages_receiver_read
    ON agent_messages(receiver_id, read);
CREATE INDEX IF NOT EXISTS idx_agent_messages_created
    ON agent_messages(created_at);
-- === (Optional) Foreign Key Constraints - Uncomment if agents table exists ===
-- ALTER TABLE agent_messages
--     ADD CONSTRAINT fk_messages_sender
--     FOREIGN KEY (sender_id) REFERENCES agents(id)
--     ON DELETE CASCADE;
-- ALTER TABLE agent_messages
--     ADD CONSTRAINT fk_messages_receiver
--     FOREIGN KEY (receiver_id) REFERENCES agents(id)
--     ON DELETE CASCADE;
-- === Audit Trigger for updated_at (Optional) ===
-- 
-- 
-- DROP TRIGGER IF EXISTS trg_update_messages_updated_at ON agent_messages;
-- 
-- === Row Level Security Policy (Optional Example) ===
-- 
-- 
-- === Example Insert for Testing ===
INSERT INTO agent_messages
    (sender_id, receiver_id, content, msg_type, metadata)
VALUES
    (1, 2, 'Requesting goal sync for upcoming cycle.', 'sync', '{"urgency": "low"}');
-- === End Migration ===
COMMIT;
COMMIT;
