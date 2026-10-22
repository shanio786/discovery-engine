-- ==================================================================
-- TimescaleDB hypertable conversion for topic_metrics.
-- Run AFTER 001_schema.sql. Safe to re-run (idempotent).
-- Momentum/acceleration detection (Phase 2) needs clean daily
-- time-series, so topic_metrics is the one hypertable that matters.
-- ==================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Convert to hypertable on the `date` dimension. The UNIQUE constraint in
-- 001 already includes `date`, which Timescale requires. 1-month chunks are
-- plenty for daily data at this corpus size.
SELECT create_hypertable(
    'topic_metrics',
    'date',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE,
    migrate_data => TRUE
);

-- Fast lookups for the scorer: "all metrics for a topic+market up to cutoff".
CREATE INDEX IF NOT EXISTS idx_topic_metrics_lookup
    ON topic_metrics (topic_id, market, source, metric_name, date DESC);
