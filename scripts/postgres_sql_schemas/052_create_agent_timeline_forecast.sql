-- scripts/migrations/002_create_agent_timeline_forecast.sql
-- Version: 2.0
-- Created: 2025-07-29
-- Description: Schema for storing simulated agent timeline forecasts.

BEGIN;

-- === Table Definition ===

CREATE TABLE IF NOT EXISTS public.agent_timeline_forecast (
    id SERIAL PRIMARY KEY,
    agent_id INTEGER NOT NULL,
    trust_score DOUBLE PRECISION CHECK (trust_score >= 0.0 AND trust_score <= 1.0),
    horizon_minutes INTEGER NOT NULL DEFAULT 60,
    simulated_events JSONB NOT NULL,
    simulation_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metadata JSONB DEFAULT '{}'::jsonb
);

-- === Comments for Full Traceability ===

COMMENT ON TABLE public.agent_timeline_forecast IS
    'Stores simulated agent goal timelines for planning, reflection, or debugging. Includes trust score, simulation horizon, and event sequence.';

COMMENT ON COLUMN public.agent_timeline_forecast.id IS
    'Primary key (autoincrementing forecast record ID).';
COMMENT ON COLUMN public.agent_timeline_forecast.agent_id IS
    'Agent being simulated (should FK to agents table if exists).';
COMMENT ON COLUMN public.agent_timeline_forecast.trust_score IS
    'Agent trust score at time of simulation (used to estimate goal duration).';
COMMENT ON COLUMN public.agent_timeline_forecast.horizon_minutes IS
    'How far into the future the simulation extends.';
COMMENT ON COLUMN public.agent_timeline_forecast.simulated_events IS
    'JSONB array of forecasted events: goals, timestamps, durations.';
COMMENT ON COLUMN public.agent_timeline_forecast.simulation_time IS
    'When this forecast was generated.';
COMMENT ON COLUMN public.agent_timeline_forecast.metadata IS
    'Flexible JSONB for source context, config, agent version, or notes.';

-- === Indexes ===

CREATE INDEX IF NOT EXISTS idx_timeline_forecast_agent_time
    ON public.agent_timeline_forecast(agent_id, simulation_time DESC);

-- === (Optional) Foreign Key Constraint - Uncomment if agents table exists ===
-- ALTER TABLE public.agent_timeline_forecast
--     ADD CONSTRAINT fk_timeline_forecast_agent
--     FOREIGN KEY (agent_id) REFERENCES public.agents(id)
--     ON DELETE CASCADE;

-- === Example Insert for Testing ===

INSERT INTO public.agent_timeline_forecast
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
