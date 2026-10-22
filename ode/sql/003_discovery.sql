-- ==================================================================
-- Phase 1.5: discovery, cross-language topic identity, watchlist.
-- Idempotent ALTER/CREATE so it applies cleanly on a DB that already
-- ran Phase 1 (no data loss) as well as on a fresh one.
-- ==================================================================

-- Stable, language-agnostic identity for a topic:
--   'wd:Q12345'      -> a Wikidata entity (same topic in every language)
--   'seed:<key>'     -> one of the operator's seed topics (config.yaml)
ALTER TABLE topics ADD COLUMN IF NOT EXISTS topic_key    TEXT;
ALTER TABLE topics ADD COLUMN IF NOT EXISTS wikidata_qid TEXT;
-- Wikipedia languages that have an article for this topic. A topic that
-- exists in 'en' but not in 'de' is itself a cross-language gap signal.
ALTER TABLE topics ADD COLUMN IF NOT EXISTS wiki_langs   TEXT[];
ALTER TABLE topics ADD COLUMN IF NOT EXISTS kind         TEXT;     -- e.g. Wikidata P31 label
CREATE UNIQUE INDEX IF NOT EXISTS uq_topics_topic_key    ON topics(topic_key);
CREATE UNIQUE INDEX IF NOT EXISTS uq_topics_wikidata_qid ON topics(wikidata_qid);

-- What each collector tracks per market. Seeds + automatically discovered
-- topics + cross-language expansions. Capped and evicted by discovery.
CREATE TABLE IF NOT EXISTS watchlist (
    market          TEXT    NOT NULL,
    kind            TEXT    NOT NULL,            -- 'wiki_article'
    term            TEXT    NOT NULL,            -- article title in that market's language
    topic_key       TEXT,                        -- NULL only for excluded non-articles
    origin          TEXT    NOT NULL,            -- 'seed' | 'wiki_top' | 'morelike' | 'langlink'
    first_seen      DATE    NOT NULL DEFAULT CURRENT_DATE,
    last_seen       DATE    NOT NULL DEFAULT CURRENT_DATE,
    hits            INTEGER NOT NULL DEFAULT 1,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    excluded_reason TEXT,
    PRIMARY KEY (market, kind, term)
);
CREATE INDEX IF NOT EXISTS idx_watchlist_active ON watchlist(market, kind) WHERE active;
CREATE INDEX IF NOT EXISTS idx_watchlist_topic  ON watchlist(topic_key);

-- Run status gains 'partial' (some items failed, rest persisted).
COMMENT ON COLUMN collector_runs.status IS 'running|ok|partial|skipped|error';
