# Opportunity Discovery Engine (ODE)

Cross-platform demand-arbitrage intelligence. ODE harvests free signals across
platforms and languages, finds topics where **real demand is rising but nobody
is serving it well**, and (in later phases) scores, verifies and turns them
into executable content / app briefs.

Not an SEO tool. SEO tools answer "how hard is this keyword?". ODE answers
"where is there unserved demand, how fast is the window closing, and is there
money there?"

---

## Status: Phase 1 (harvest + discovery) ✅

| Area | What exists |
|---|---|
| **Discovery** | Finds topics nobody configured: each language's daily top-1000 Wikipedia articles (mainstream breakouts) + "morelike" neighbours of every seed (niche / tutorial territory). People, films, sports, elections, list pages etc. are filtered out via Wikidata classes. |
| **Cross-language identity** | Every topic is a Wikidata entity: *Heat pump* (en) and *Wärmepumpe* (de) are **one** topic with per-market metrics. Every discovered topic is automatically tracked in every other market. `topics.wiki_langs` records which languages have an article at all (a gap signal on its own). |
| **Collectors** | Wikipedia pageviews (demand backbone, human traffic only), Hacker News, YouTube, Reddit. One uniform interface. |
| **Correct data** | Incremental fetching with overlap; interior zero-days filled; HN counts are exact per day (no 1000-hit truncation); Reddit only writes fully-covered days; "unanswered" only judged after 48h; each market gets its own Reddit scope and localized queries (no English noise in de-DE). |
| **Resilience** | Per-item failure isolation, retries only for transient errors, Retry-After on 429, circuit breaker per host, persisted YouTube quota with least-recently-searched rotation, advisory lock against overlapping cron jobs, orphaned runs closed. |
| **Security** | DB bound to `127.0.0.1` with a password from `.env`; API keys redacted from every error/log; container runs as non-root. |
| **Storage** | Postgres 16 + TimescaleDB hypertable, compression after 90 days, bulk COPY upserts, nightly `pg_dump` backups. |
| **Ops** | `ode doctor` checks config, DB and every live API in seconds; every run recorded in `collector_runs` as ok / partial / skipped / error. |
| **Tests** | 91 tests: parsers, HTTP behaviour, config validation, and integration tests on a real TimescaleDB including a full discovery → collection run. CI on every push. |

Cost: **$0** beyond the VPS. Wikipedia, Wikidata and Hacker News need no keys;
YouTube and Reddit are free and skip themselves cleanly until keys are set.

---

## VPS setup (Roman Urdu)

```bash
# 1. Code VPS par
sudo git clone -b claude/practical-dirac-cy1u0p https://github.com/shanio786/discovery-engine /opt/discovery-engine
cd /opt/discovery-engine

# 2. Secrets: password zaroor badlo
cp .env.example .env && chmod 600 .env
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
nano .env        # YOUTUBE_API_KEY, REDDIT_CLIENT_ID/SECRET (jab mil jayein)

# 3. Database + image + self-check
./scripts/deploy.sh      # git pull, build, migrations, `ode doctor`

# 4. Pehla poora run (pehli dafa 10-20 minute: ~13 mahine ki history aati hai)
docker compose run --rm app ode daily

# 5. Dekho kya aaya
docker compose run --rm app ode status
docker compose run --rm app ode watchlist

# 6. Roz ka automatic schedule + backups
crontab cron/crontab
```

`ode doctor` mein sab `[OK]` aaye to system sahi chal raha hai. YouTube/Reddit
ke `[warn]` sirf keys na hone ki wajah se hain.

Naya code aaye to bas `./scripts/deploy.sh` chalao.

---

## Commands

| Command | What it does |
|---|---|
| `ode doctor` | Config + DB + one tiny live request to every API, parsed with the real parsers. Run after every deploy. |
| `ode daily` | Schema → discover → collect, under a lock. What cron runs. |
| `ode discover [--market de-DE]` | Refresh watchlists only. |
| `ode collect [--source wikipedia] [--market en-US]` | Collect only. |
| `ode status` | Recent runs + a per-source/market data summary. |
| `ode watchlist` | Tracked topics per market and origin (seed / wiki_top / morelike / langlink). |
| `ode initdb` | Apply migrations (idempotent; safe on an existing Phase-1 database). |

Exit codes: `0` ok, `1` a run errored / a required check failed, `2` bad
config, `3` another harvest is running.

## Configuration

Everything tunable is in [`config/config.yaml`](config/config.yaml) and is
validated on load:

- **markets**: start with `en-US, de-DE`; add from `all_markets`.
- **seeds**: your niches. `wiki` = English article title (other languages
  are resolved via Wikidata); `queries` per language; `hn` = short core term.
- **discovery**: how many days of top articles, morelike depth, watchlist cap,
  staleness, excluded Wikidata classes and title patterns.
- **reddit.scopes**: which subreddits represent each market.
- rate limits, concurrency, quotas, retries and circuit breaker.

Adding a market = add it to `markets`, add `queries` in that language to the
seeds you care about, and (optionally) a Reddit scope. Wikipedia discovery
works for any language automatically.

## Architecture

```
            config.yaml + .env
                    │
   ┌────────────────▼─────────────────┐
   │ discovery (per market)           │  top-1000 + morelike(seeds)
   │  filter → Wikidata classes →     │  → watchlist (seeds never evicted,
   │  langlinks to every other market │     cap + staleness eviction)
   └────────────────┬─────────────────┘
                    ▼
   ┌──────────────────────────────────┐
   │ collectors × markets             │  HttpFetcher: rate limit, retries,
   │  wikipedia  hackernews           │  Retry-After, circuit breaker,
   │  youtube    reddit               │  secret redaction, quota
   └────────────────┬─────────────────┘
                    ▼ COPY → staging → INSERT … ON CONFLICT
   Postgres 16 + TimescaleDB
   topics(topic_key, wikidata_qid, wiki_langs) · topic_aliases
   topic_metrics (hypertable, compressed >90d) · watchlist
   videos · channels · collector_runs · api_quota
```

Topic identity: `wd:Q…` (Wikidata entity), `seed:<key>` (your seeds; a seed
absorbs its Wikidata entity's history if discovery saw it first).

## Development

```bash
pip install -e ".[dev]"
ruff check ode tests
pytest -q                                   # unit tests (no network, no DB)
docker compose up -d db                     # needs POSTGRES_PASSWORD in .env
ODE_TEST_DATABASE_URL=postgresql://ode:<pw>@localhost:5432/ode pytest -q   # + DB tests
```

## Roadmap

1. **Harvest + discovery** ← done
2. Momentum/acceleration + outlier scoring + composite v1
3. FastAPI + dashboard
4. DataForSEO verify layer (hard budget cap)
5. RunPod embeddings + HDBSCAN clustering + cross-language arbitrage view
6. Serial outlier miner + format extraction
7. Backtest harness + weight optimization
8. Portfolio optimizer + alerts + brief generator
