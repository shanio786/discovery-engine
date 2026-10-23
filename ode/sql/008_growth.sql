-- ==================================================================
-- Growth layer: your work log (learning), long-tail questions, Play Store
-- complaints, mined viral formats. Idempotent.
-- ==================================================================

-- What YOU did with a plan item, and what it earned. Drives "done" filtering
-- and calibration (real revenue per visitor per market / platform).
CREATE TABLE IF NOT EXISTS work_log (
    action_id   TEXT PRIMARY KEY,          -- stable id shown in the plan
    topic_id    BIGINT REFERENCES topics(id) ON DELETE SET NULL,
    topic       TEXT NOT NULL,
    market      TEXT NOT NULL,
    platform    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'started',   -- started | published | dropped
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    hours       DOUBLE PRECISION,
    views       BIGINT,
    revenue_usd DOUBLE PRECISION,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_work_log_topic ON work_log (topic_id, market, platform);

-- Long-tail questions from Google Autocomplete (per topic, per market).
CREATE TABLE IF NOT EXISTS longtail (
    topic_id    BIGINT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    market      TEXT NOT NULL,
    suggestion  TEXT NOT NULL,
    seed        TEXT,                      -- the prefix that produced it
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (topic_id, market, suggestion)
);

-- 1-star Play Store reviews of the top apps for a topic.
CREATE TABLE IF NOT EXISTS app_complaints (
    topic_id    BIGINT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    market      TEXT NOT NULL,
    app_id      TEXT NOT NULL,
    review_id   TEXT NOT NULL,
    content     TEXT NOT NULL,
    at          TIMESTAMPTZ,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (topic_id, market, app_id, review_id)   -- one app can serve many topics
);

-- Repeatable formats mined from serial-outlier channels.
CREATE TABLE IF NOT EXISTS formats (
    channel_id    TEXT PRIMARY KEY REFERENCES channels(id) ON DELETE CASCADE,
    topic_id      BIGINT REFERENCES topics(id) ON DELETE SET NULL,
    market        TEXT,
    outlier_count INTEGER NOT NULL,
    avg_multiple  DOUBLE PRECISION,
    titles        JSONB NOT NULL,
    formula       TEXT,                    -- Gemini: title formula + structure
    mined_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Snapshot of every plan item shown, so `ode done <id>` always resolves.
CREATE TABLE IF NOT EXISTS plan_items (
    action_id  TEXT PRIMARY KEY,
    day        DATE NOT NULL,
    topic_id   BIGINT REFERENCES topics(id) ON DELETE SET NULL,
    topic      TEXT NOT NULL,
    market     TEXT NOT NULL,
    platform   TEXT NOT NULL,
    score      DOUBLE PRECISION,
    payload    JSONB
);

-- Learned settings (e.g. backtest-optimised composite weights).
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Backtest runs: did the scorer's top-20 really grow? (precision@k vs random)
CREATE TABLE IF NOT EXISTS backtest_runs (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    cutoff      DATE NOT NULL,
    horizon     INTEGER NOT NULL,
    market      TEXT,                      -- NULL = all markets pooled
    universe    INTEGER NOT NULL,          -- topics evaluated
    k           INTEGER NOT NULL,
    hits        INTEGER NOT NULL,          -- grew in the top-k
    base_rate   DOUBLE PRECISION NOT NULL, -- share that grew in the whole universe
    precision_k DOUBLE PRECISION NOT NULL,
    lift        DOUBLE PRECISION,          -- precision_k / base_rate
    weights     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_run ON backtest_runs (run_at DESC);

-- n8n / webhook exports already delivered (one per day per target).
CREATE TABLE IF NOT EXISTS exports_sent (
    day     DATE NOT NULL,
    target  TEXT NOT NULL,
    items   INTEGER NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (day, target)
);
