# Opportunity Discovery Engine (ODE)

Cross-platform demand-arbitrage intelligence. It harvests free signals across
many platforms and languages, finds topics where **real demand is rising but
nobody is serving it well**, and (in later phases) scores, verifies and turns
them into executable content/app briefs.

Not an SEO tool. SEO tools answer "how hard is this keyword?" ODE answers
"where is there unserved demand, how fast is the window closing, and is there
money there?"

---

## Status: Phase 1 — Harvest layer ✅

| | |
|---|---|
| **Schema** | Full schema designed (`sql/001_schema.sql`); `topic_metrics` is a TimescaleDB hypertable (`sql/002_hypertables.sql`) |
| **Collectors** | Wikipedia (demand backbone), Hacker News, YouTube, Reddit — one uniform `Collector` interface |
| **Ops** | Idempotent upserts, per-source rate limiting + backoff, **persisted daily quota caps**, structured logging, every run recorded in `collector_runs` |
| **Config** | Markets, seeds, rate limits, quotas, thresholds in `config/config.yaml` (not code) |
| **Secrets** | Environment only (`.env`), never committed |
| **Tests** | Scoring-math-ready; Phase-1 covers rate limiter, quota, models, collector parsing (no network/DB needed) |

Phase 1 runs with **zero API keys** for Wikipedia + Hacker News. YouTube and
Reddit **self-skip gracefully** (recorded as `skipped` in `collector_runs`)
until you add keys — the pipeline never fails because a key is missing.

---

## Quick start

### 1. Local (Python 3.11+)

```bash
pip install -e ".[dev]"      # install package + dev tools
cp .env.example .env         # fill in keys later; works empty for wiki/HN
pytest -q                    # run the test suite (no network/DB needed)
```

### 2. With the full stack (Docker)

```bash
cp .env.example .env
docker compose up -d db                       # start TimescaleDB
docker compose run --rm app ode initdb        # apply schema + hypertable
docker compose run --rm app ode collect       # run all collectors x all markets
docker compose run --rm app ode status        # see what ran
```

---

## Verifying Phase 1 works

```bash
# 1. Tests pass
pytest -q

# 2. Schema applies cleanly (needs the db up)
docker compose run --rm app ode initdb
#    -> logs "schema.ready hypertable=True"

# 3. Harvest flows (Wikipedia + HN work with no keys)
docker compose run --rm app ode collect --source wikipedia
docker compose run --rm app ode collect --source hackernews --market en-US

# 4. Data landed
docker compose exec db psql -U ode -d ode -c \
  "SELECT source, count(*) FROM topic_metrics GROUP BY source;"

# 5. Runs were recorded (ok / skipped / error, with quota)
docker compose run --rm app ode status

# 6. Idempotency — re-run and confirm counts don't double
docker compose run --rm app ode collect --source wikipedia
docker compose exec db psql -U ode -d ode -c "SELECT count(*) FROM topic_metrics;"
```

Adding keys later: put `YOUTUBE_API_KEY` / `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`
in `.env`, re-run `ode collect`, and those sources flip from `skipped` to `ok`.

---

## Architecture

```
config/config.yaml ──┐
.env (secrets) ──────┤
                     ▼
     ┌─────────────────────────────────┐
     │          pipeline.harvest        │  per (collector × market)
     │  ┌───────────────────────────┐   │
     │  │ WikipediaCollector        │   │  rate-limit + backoff
     │  │ HackerNewsCollector       │   │  persisted quota cap
     │  │ YouTubeCollector          │   │  → list[RawSignal]
     │  │ RedditCollector           │   │
     │  └───────────────────────────┘   │
     └───────────────┬─────────────────┘
                     ▼ idempotent upsert
      Postgres 16 + TimescaleDB
      topics / topic_aliases / topic_metrics(hypertable)
      videos / channels / collector_runs / api_quota
```

Each collector implements the same interface, so new sources (Phase 2+:
StackExchange, GitHub, Product Hunt, …) drop in by adding one file and one line
in `ode/collectors/__init__.py`.

## Cost

Phase 1 cost: **$0** beyond the VPS you already run. All sources are free.
Paid data (DataForSEO) only arrives in Phase 4, hard-capped to the top ~20
candidates/day — never the raw firehose.

## Roadmap

1. **Harvest layer** ← you are here
2. Momentum/acceleration + outlier scoring + composite v1
3. FastAPI + dashboard
4. DataForSEO verify layer (hard budget cap)
5. RunPod embeddings + HDBSCAN clustering + cross-language arbitrage
6. Serial outlier miner + format extraction
7. Backtest harness + weight optimization
8. Portfolio optimizer + alerts + brief generator
