-- ode:optional  (TimescaleDB native compression; skipped with a warning if unsupported)
-- Compress topic_metrics chunks older than 90 days. On a 16GB VPS this keeps
-- years of daily history small. Recent chunks stay uncompressed for fast upserts.
ALTER TABLE topic_metrics SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'topic_id, market, source, metric_name',
    timescaledb.compress_orderby   = 'date DESC'
);
SELECT add_compression_policy('topic_metrics', INTERVAL '90 days', if_not_exists => TRUE);
