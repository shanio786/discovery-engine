-- ==================================================================
-- Realtime layer: hourly Wikipedia views, Google Trends items, alert log.
-- Idempotent.
-- ==================================================================

-- Hourly pageviews (from the public hourly dumps), only for articles above a
-- views threshold, kept a few days for spike detection.
CREATE TABLE IF NOT EXISTS hourly_views (
    wiki   TEXT        NOT NULL,     -- 'en.wikipedia'
    title  TEXT        NOT NULL,     -- spaces, not underscores
    hour   TIMESTAMPTZ NOT NULL,
    views  INTEGER     NOT NULL,
    UNIQUE (wiki, title, hour)
);
CREATE INDEX IF NOT EXISTS idx_hourly_views_lookup ON hourly_views (wiki, hour DESC);

-- Which hourly files were already ingested (so restarts never double count).
CREATE TABLE IF NOT EXISTS hourly_ingested (
    hour        TIMESTAMPTZ PRIMARY KEY,
    rows        INTEGER NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Google Trends "trending now" per market.
CREATE TABLE IF NOT EXISTS trend_items (
    market      TEXT NOT NULL,
    title       TEXT NOT NULL,
    traffic     INTEGER,                 -- approx searches ("200+" -> 200)
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    peak_traffic INTEGER,
    wiki_title  TEXT,                    -- matched Wikipedia article, if any
    PRIMARY KEY (market, title)
);

-- Every alert sent, so the same thing never pings twice within its cooldown.
CREATE TABLE IF NOT EXISTS alerts_sent (
    key      TEXT PRIMARY KEY,           -- e.g. 'breakout:en-US:wd:Q42'
    kind     TEXT NOT NULL,
    sent_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload  JSONB
);

-- Last run of each scheduler job (survives restarts; nothing re-runs early).
CREATE TABLE IF NOT EXISTS scheduler_state (
    job      TEXT PRIMARY KEY,
    last_run TIMESTAMPTZ NOT NULL
);
