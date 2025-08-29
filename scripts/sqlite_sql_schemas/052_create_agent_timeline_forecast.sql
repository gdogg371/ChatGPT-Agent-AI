-- scripts/migrations/002_create_agent_timeline_forecast.sql
-- Version: 2.0
-- Created: 2025-07-29
-- Description: Schema for storing simulated agent timeline forecasts.
BEGIN;
-- === Table Definition ===
CREATE TABLE IF NOT EXISTS agent_timeline_forecast (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL,
    trust_score REAL CHECK (trust_score >= 0.0 AND trust_score <= 1.0),
    horizon_minutes INTEGER NOT NULL DEFAULT 60,
    simulated_events TEXT NOT NULL,
    simulation_time TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metadata TEXT DEFAULT '{}');
-- === Comments for Full Traceability ===
-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_timeline_forecast_agent_time
    ON agent_timeline_forecast(agent_id, simulation_time DESC);
-- === (Optional) Foreign Key Constraint - Uncomment if agents table exists ===
-- ALTER TABLE agent_timeline_forecast
--     ADD CONSTRAINT fk_timeline_forecast_agent
--     FOREIGN KEY (agent_id) REFERENCES agents(id)
--     ON DELETE CASCADE;
-- === Example Insert for Testing ===
INSERT INTO agent_timeline_forecast
    (agent_id, trust_score, horizon_minutes, simulated_events)
VALUES
    (
        1,
        0.85,
        90,
        '[{"timestamp": "2025-07-29T10:00:00Z", "goal_id": 42, "description": "Scan directories", "estimated_duration": 10, "trust_score": 0.85}]'
    );
-- === End Migration ===
COMMIT;
