-- ==================================================================
-- Phase 2: scoring outputs + YouTube probe queue. Idempotent.
-- ==================================================================

ALTER TABLE scores ADD COLUMN IF NOT EXISTS confidence   DOUBLE PRECISION;
ALTER TABLE scores ADD COLUMN IF NOT EXISTS velocity     DOUBLE PRECISION;
ALTER TABLE scores ADD COLUMN IF NOT EXISTS acceleration DOUBLE PRECISION;
ALTER TABLE scores ADD COLUMN IF NOT EXISTS spike_ratio  DOUBLE PRECISION;
ALTER TABLE scores ADD COLUMN IF NOT EXISTS money_path   TEXT;
ALTER TABLE scores ADD COLUMN IF NOT EXISTS details      JSONB;
ALTER TABLE scores ADD COLUMN IF NOT EXISTS computed_at  TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS idx_scores_rank ON scores (market, date, composite DESC);

ALTER TABLE monetization ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Top-scoring discovered topics that have no YouTube data yet are queued here
-- by the scorer; the YouTube collector probes them on its next run (free quota)
-- so tomorrow's score has real GAP / COMPETITION data instead of neutral values.
CREATE TABLE IF NOT EXISTS probe_queue (
    market     TEXT NOT NULL,
    topic_key  TEXT NOT NULL,
    query      TEXT NOT NULL,
    reason     TEXT,
    score      DOUBLE PRECISION,
    queued_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    probed_at  TIMESTAMPTZ,
    PRIMARY KEY (market, topic_key)
);
CREATE INDEX IF NOT EXISTS idx_probe_pending ON probe_queue (market, queued_at) WHERE probed_at IS NULL;
