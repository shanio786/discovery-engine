# Opportunity Discovery Engine (ODE)

Cross-platform demand-arbitrage intelligence. ODE harvests free signals across
platforms and languages, finds topics where **real demand is rising but nobody
is serving it well**, and (in later phases) scores, verifies and turns them
into executable content / app briefs.

Not an SEO tool. SEO tools answer "how hard is this keyword?". ODE answers
"where is there unserved demand, how fast is the window closing, and is there
money there?"

---

## Status: Phases 1–2 (harvest, discovery, scoring) ✅

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
| **Scoring** | Momentum by acceleration (spec model) with spike persistence and a significance test, demand / money / gap / competition, composite, confidence, saturation forecast, cross-language gaps. Top discovered topics are queued for a free YouTube check the next day. |
| **Output** | `ode gems`: a ranked shortlist with a plain-language *why* for every row (terminal, Markdown, CSV); written daily to `reports/latest.md`. |
| **Tests** | 133 tests: parsers, HTTP behaviour, config, momentum edge cases (sparse, single spikes, all-zeros, noise robustness), every score component, and integration tests on a real TimescaleDB (discovery → collection → scoring, no-lookahead guarantee). CI on every push. |

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

`ode doctor` mein sab `[OK]` aaye to system sahi chal raha hai.

### Roz kya dekhna hai

```bash
cat /opt/discovery-engine/reports/latest.md          # aaj ki gems list
docker compose run --rm app ode gems                  # terminal mein
docker compose run --rm app ode gems --market de-DE --min-confidence 0.5
```

Har gem ke saath **"why"** likha hota hai: kis phase mein hai (🚀 BREAKOUT /
📈 GROWING / ⛰ PEAKING / ➖ STABLE / 📉 DECLINING), roz kitne log dhoond rahe
hain, paisa kis raste se (SPONSOR / PRODUCT / APP / ADS), YouTube par kitna
competition hai, aur kis language mein abhi article hi nahi hai.

- **Score** = momentum × (demand, money, gap, competition ka mean). BREAKOUT 3×, DECLINING 0.3×.
- **Confidence** 0–1: kitna asli data peeche hai. 0.4 se kam = sirf Wikipedia
  data; YouTube probe ke baad barh jata hai.
- **Saturation**: OPEN (12+ mahine) / NARROWING (~6) / CLOSING_FAST (~2,
  abhi ya kabhi nahi). Ye YouTube ke 12 mahine ke uploads se banta hai, isliye
  YouTube key lagne ke ~1 hafte baad aana shuru hota hai.
- Pehle hafte zyada tar topics **UNKNOWN** honge (60 din ki history chahiye).
  Pehla run 13 mahine ki Wikipedia history le aata hai, to asal mein ye foran
  bharta hai; sirf naye discover hue topics ko waqt lagta hai.
- Scores abhi **validated nahi** (Phase 7 backtest baqi hai). List ko shortlist
  samjho jise aap khud verify karo, pakki prediction nahi. YouTube/Reddit
ke `[warn]` sirf keys na hone ki wajah se hain.

Naya code aaye to bas `./scripts/deploy.sh` chalao.

---

## Commands

| Command | What it does |
|---|---|
| `ode doctor` | Config + DB + one tiny live request to every API, parsed with the real parsers. Run after every deploy. |
| `ode daily` | Schema → discover → collect → score, under a lock. What cron runs. |
| `ode gems [--market de-DE] [--format table\|md\|csv] [--limit 30] [--min-confidence 0.5]` | The ranked shortlist with a *why* per row. `--include-unknown` also lists topics too new for a momentum phase. |
| `ode score [--as-of YYYY-MM-DD]` | Re-score from stored data (only rows dated ≤ as-of are read). |
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

## Scoring model

| Part | Range | How |
|---|---|---|
| Momentum | ×0.3–×3.0 | Spec phases on velocity (slope/mean) and acceleration (change in slope between halves). Short spikes removed by a trailing median (a rise must hold `spike_persist_days`); both half-slopes come from one quadratic fit on weekly medians, and the curvature must be ≥ 2 standard errors from zero to count — real Wikipedia noise otherwise flips steady growth into BREAKOUT. < 60 days or no demand → UNKNOWN (×1.0). |
| Demand | 0–100 | Wikipedia 30-day mean, Reddit 90-day posts, HN 90-day stories; each log + rank normalized **within the market**; weights 0.5/0.3/0.2, renormalized over what exists. |
| Money | 0–100 | Eligible paths (ADS always; PRODUCT/APP by intent keywords incl. German; SPONSOR for `audience: b2b` seeds or professional Wikidata classes); blended revenue per visitor, log-scaled. Comparison/review intent ×0.7. No affiliate path. |
| Gap | 0–100 | Small-channel outlier strength (views/subs), no channel owning > 30% of top videos, share of unanswered Reddit posts. |
| Competition | 1–100 | YouTube videos published on the topic in the last 180 days, log-normalized within the market. |
| Composite | | momentum × weighted geometric mean(demand, money, gap, 101 − competition). Missing gap/competition = neutral 50 (lowers confidence, never zeros the score). |
| Confidence | 0–1 | 40% history length + 60% how many parts rest on real data; UNKNOWN capped at 0.3. |
| Saturation | | Spec formula on monthly YouTube uploads: last 3 months vs months 10–12 ago → OPEN / NARROWING / CLOSING_FAST; UNKNOWN until those months exist (never guessed). |

Every scoring read goes through `MetricReader`, which requires an `as_of` date
and selects only rows with `date <= as_of`, so the Phase-7 backtest can reuse
the scorer with zero lookahead.

**YouTube quota (10k/day free):** seeds every 3 days (~102 units each), up to 8
probes per market (top discovered topics without YouTube data), then a one-off
backfill of monthly upload counts (100 units per month per topic, each month
fetched once). Hard-capped at 9,000/day; anything left over waits for tomorrow.

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
2. **Momentum/acceleration + outlier scoring + composite v1** ← done
3. FastAPI + dashboard
4. DataForSEO verify layer (hard budget cap)
5. RunPod embeddings + HDBSCAN clustering + cross-language arbitrage view
6. Serial outlier miner + format extraction
7. Backtest harness + weight optimization
8. Portfolio optimizer + alerts + brief generator
