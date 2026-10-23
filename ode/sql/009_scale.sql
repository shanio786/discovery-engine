-- ==================================================================
-- Scale layer: your published content and its daily stats, competitor
-- events, social trends (TikTok / YouTube), topic embeddings + niches,
-- per-language packs, DataForSEO extras. Idempotent.
-- ==================================================================

-- Things YOU published (a YouTube video / short, a blog post, a tool page, an app).
CREATE TABLE IF NOT EXISTS my_content (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    url            TEXT NOT NULL UNIQUE,
    platform       TEXT NOT NULL,              -- YOUTUBE | SHORTS | BLOG | TOOLSITE | APP
    external_id    TEXT,                       -- video id / app id
    action_id      TEXT,                       -- the plan item it came from
    topic_id       BIGINT REFERENCES topics(id) ON DELETE SET NULL,
    market         TEXT,
    keyword        TEXT,                       -- Google keyword to rank for (web)
    title          TEXT,
    published_at   TIMESTAMPTZ,
    added_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    active         BOOLEAN NOT NULL DEFAULT true,
    verdict        TEXT,                       -- WINNER | PROMISING | EVERGREEN | FLAT | DROP | TOO_EARLY
    verdict_at     TIMESTAMPTZ,
    verdict_detail JSONB
);

-- Daily snapshot of each piece of content (cumulative numbers).
CREATE TABLE IF NOT EXISTS content_stats (
    content_id BIGINT NOT NULL REFERENCES my_content(id) ON DELETE CASCADE,
    date       DATE NOT NULL,
    views      BIGINT,
    likes      BIGINT,
    comments   BIGINT,
    position   INTEGER,              -- Google position of the keyword (web content)
    installs   BIGINT,
    rating     DOUBLE PRECISION,
    PRIMARY KEY (content_id, date)
);

-- Competitor events (a big channel entered, an upload surge, a new SERP leader).
CREATE TABLE IF NOT EXISTS competitor_events (
    key      TEXT PRIMARY KEY,
    topic_id BIGINT REFERENCES topics(id) ON DELETE CASCADE,
    market   TEXT,
    kind     TEXT NOT NULL,          -- big_channel | upload_surge | serp_newcomer
    detail   JSONB,
    seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_competitor_events_topic ON competitor_events (topic_id, market);

-- Social trends: TikTok hashtags / sounds, YouTube most-popular videos.
CREATE TABLE IF NOT EXISTS social_trends (
    source      TEXT NOT NULL,       -- tiktok | youtube
    market      TEXT NOT NULL,
    kind        TEXT NOT NULL,       -- hashtag | sound | video
    name        TEXT NOT NULL,
    rank        INTEGER,
    metric      DOUBLE PRECISION,    -- posts / views
    rank_change INTEGER,
    detail      JSONB,
    wiki_title  TEXT,
    topic_id    BIGINT REFERENCES topics(id) ON DELETE SET NULL,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, market, kind, name)
);

-- Topic embeddings (for niche clustering); recomputed only when the text changes.
CREATE TABLE IF NOT EXISTS topic_embeddings (
    topic_id    BIGINT PRIMARY KEY REFERENCES topics(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    text        TEXT NOT NULL,
    vector      REAL[] NOT NULL,
    embedded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS detail JSONB;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS signature TEXT;   -- member ids hash (label cache)

-- One production in many languages: keyword, titles, questions per language.
CREATE TABLE IF NOT EXISTS language_packs (
    action_id    TEXT NOT NULL,
    language     TEXT NOT NULL,
    keyword      TEXT,
    titles       JSONB NOT NULL,
    questions    JSONB NOT NULL,
    verified     JSONB,              -- what Google Autocomplete really suggests
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (action_id, language)
);

-- DataForSEO extras.
ALTER TABLE longtail ADD COLUMN IF NOT EXISTS volume INTEGER;     -- Google monthly searches
ALTER TABLE serp_results ADD COLUMN IF NOT EXISTS url TEXT;
ALTER TABLE serp_results ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE serp_results ADD COLUMN IF NOT EXISTS keyword TEXT;
CREATE INDEX IF NOT EXISTS idx_serp_results_recent ON serp_results (topic_id, market, fetched_at DESC);
