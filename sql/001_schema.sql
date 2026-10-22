-- ==================================================================
-- Opportunity Discovery Engine — full schema
-- Phase 1 populates: topics, topic_aliases, topic_metrics, videos,
--                    channels, collector_runs, api_quota
-- Later phases populate the rest (clusters, scores, monetization,
--                    serp_results, briefs, backtests, portfolio).
-- The whole schema is designed up front (spec requirement) so later
-- phases only fill tables, never migrate core shape.
-- ==================================================================

-- ---------- Core entity: a topic is language-agnostic --------------
CREATE TABLE IF NOT EXISTS topics (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    cluster_id     BIGINT,                 -- filled in Phase 5 (clustering)
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- How a topic is phrased per language/market. Resolution key for collectors.
CREATE TABLE IF NOT EXISTS topic_aliases (
    topic_id     BIGINT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    market       TEXT   NOT NULL,          -- e.g. 'en-US'
    surface_form TEXT   NOT NULL,          -- phrasing as seen on that platform/lang
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (market, surface_form)     -- one alias string per market -> one topic
);
CREATE INDEX IF NOT EXISTS idx_topic_aliases_topic ON topic_aliases(topic_id);

-- Time-series of every raw signal. Becomes a TimescaleDB hypertable
-- in 002_hypertables.sql. Idempotency key = (topic,market,source,metric,date).
CREATE TABLE IF NOT EXISTS topic_metrics (
    topic_id    BIGINT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    market      TEXT   NOT NULL,
    date        DATE   NOT NULL,           -- observation day (time dimension)
    source      TEXT   NOT NULL,           -- 'wikipedia' | 'youtube' | 'reddit' | 'hackernews'
    metric_name TEXT   NOT NULL,           -- 'pageviews' | 'questions' | 'outlier_ratio' ...
    value       DOUBLE PRECISION NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (topic_id, market, source, metric_name, date)
);

-- Semantic clusters (Phase 5). centroid kept as float8[] in Phase 1 to avoid a
-- pgvector dependency on the VPS; Phase 5 migrates this column to vector(1024).
CREATE TABLE IF NOT EXISTS clusters (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    label          TEXT,
    centroid       DOUBLE PRECISION[],
    size           INTEGER NOT NULL DEFAULT 0,
    coverage_score DOUBLE PRECISION,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- Platform-specific ----------
CREATE TABLE IF NOT EXISTS channels (
    id                TEXT PRIMARY KEY,     -- YouTube channel id
    title             TEXT,
    subs              BIGINT,
    total_views       BIGINT,
    country           TEXT,
    created_at        TIMESTAMPTZ,          -- channel creation (from API)
    is_serial_outlier BOOLEAN NOT NULL DEFAULT FALSE,  -- Phase 6
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS videos (
    id           TEXT PRIMARY KEY,          -- YouTube video id
    channel_id   TEXT REFERENCES channels(id) ON DELETE SET NULL,
    topic_id     BIGINT REFERENCES topics(id) ON DELETE SET NULL,
    title        TEXT,
    views        BIGINT,
    published_at TIMESTAMPTZ,
    duration     INTEGER,                   -- seconds
    market       TEXT,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_videos_topic   ON videos(topic_id);
CREATE INDEX IF NOT EXISTS idx_videos_pub     ON videos(published_at);

-- SERP snapshot (Phase 4, paid verify layer)
CREATE TABLE IF NOT EXISTS serp_results (
    topic_id         BIGINT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    market           TEXT NOT NULL,
    position         INTEGER NOT NULL,
    domain           TEXT,
    domain_authority DOUBLE PRECISION,
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (topic_id, market, position, fetched_at)
);

-- ---------- Intelligence output (Phase 2+) ----------
CREATE TABLE IF NOT EXISTS scores (
    topic_id        BIGINT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    market          TEXT NOT NULL,
    date            DATE NOT NULL,
    demand          DOUBLE PRECISION,
    momentum        DOUBLE PRECISION,
    money           DOUBLE PRECISION,
    gap             DOUBLE PRECISION,
    competition     DOUBLE PRECISION,
    saturation_risk TEXT,
    composite       DOUBLE PRECISION,
    phase           TEXT,
    PRIMARY KEY (topic_id, market, date)
);

CREATE TABLE IF NOT EXISTS monetization (
    topic_id     BIGINT PRIMARY KEY REFERENCES topics(id) ON DELETE CASCADE,
    primary_path TEXT,                      -- SPONSOR | PRODUCT | APP | ADS
    est_rpv      DOUBLE PRECISION,
    paths        JSONB
);

CREATE TABLE IF NOT EXISTS briefs (
    topic_id     BIGINT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    titles       JSONB,
    outline      TEXT,
    format       TEXT,
    platform     TEXT,
    PRIMARY KEY (topic_id, generated_at)
);

-- ---------- Ops ----------
CREATE TABLE IF NOT EXISTS collector_runs (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    collector   TEXT NOT NULL,
    market      TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    items       INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    quota_used  INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'running',  -- running|ok|skipped|error
    detail      JSONB
);
CREATE INDEX IF NOT EXISTS idx_collector_runs_started ON collector_runs(started_at DESC);

-- Persisted per-API daily quota. Collectors refuse calls that would exceed it.
CREATE TABLE IF NOT EXISTS api_quota (
    api         TEXT NOT NULL,
    date        DATE NOT NULL,
    units_used  INTEGER NOT NULL DEFAULT 0,
    daily_limit INTEGER NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (api, date)
);

CREATE TABLE IF NOT EXISTS backtests (
    run_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    cutoff_date     DATE NOT NULL,
    weights         JSONB,
    precision_at_20 DOUBLE PRECISION,
    details         JSONB,
    PRIMARY KEY (run_at, cutoff_date)
);

CREATE TABLE IF NOT EXISTS portfolio (
    topic_id       BIGINT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    bucket         TEXT,                    -- CORE | GROWTH | BET
    assigned_to    TEXT,
    status         TEXT,
    hours_spent    DOUBLE PRECISION DEFAULT 0,
    actual_revenue DOUBLE PRECISION DEFAULT 0,
    PRIMARY KEY (topic_id)
);
