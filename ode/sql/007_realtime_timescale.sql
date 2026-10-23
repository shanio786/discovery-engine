-- ode:optional  (TimescaleDB: hypertable + 4-day retention for hourly views)
SELECT create_hypertable('hourly_views', 'hour', chunk_time_interval => INTERVAL '1 day',
                         if_not_exists => TRUE, migrate_data => TRUE);
SELECT add_retention_policy('hourly_views', INTERVAL '4 days', if_not_exists => TRUE);
