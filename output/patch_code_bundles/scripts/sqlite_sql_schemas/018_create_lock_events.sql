-- scripts/migrations/018_create_lock_events.sql
BEGIN;
CREATE TABLE IF NOT EXISTS lock_events (
    id INTEGER PRIMARY KEY,
    lock_name TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('acquire', 'release', 'fail')),
    holder TEXT,
    metadata TEXT DEFAULT '{}',
    event_time TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_lock_events_name ON lock_events(lock_name);
CREATE INDEX IF NOT EXISTS idx_lock_events_time ON lock_events(event_time);
COMMIT;
