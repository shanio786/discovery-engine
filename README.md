# Opportunity Discovery Engine (ODE)

Cross-platform demand-arbitrage intelligence. ODE harvests free signals across
platforms and languages, finds topics where **real demand is rising but nobody
is serving it well**, and (in later phases) scores, verifies and turns them
into executable content / app briefs.

Not an SEO tool. SEO tools answer "how hard is this keyword?". ODE answers
"where is there unserved demand, how fast is the window closing, and is there
money there?"

---

## What you get every morning: `reports/plan-latest.md`

A ranked list of **what to do now** — platform, language, topic, why, and
the first steps — across 12 languages. Any subject qualifies: everything is
**produced with AI and automation**, so a niche only needs demand and room.

| Platform | When the engine recommends it |
|---|---|
| **AI blog / website** | People search for it; few languages cover it; or a topic proven in one language has **no article at all** in another (be first there) |
| **YouTube** (faceless AI videos) | Enough demand; few competing videos per daily searcher; small channels out-performing their size; sponsors for B2B topics |
| **Shorts · TikTok · Reels** | The topic is in BREAKOUT / GROWING — ride the trend early, one AI video on three apps |
| **AI-built web tool site** | People want to calculate / convert / generate something; free tool + ads |
| **AI-built Play Store app** | Utility intent (tracker / calculator / planner …) or the top apps are badly rated / abandoned while installs prove demand; AdMob |

Every item also lists the **other languages where the same topic is open**
(AI translation makes copying it nearly free). Steps describe the AI pipeline
(AI script → AI voice → AI visuals, AI articles → AI translation, AI-coded
tools/apps) and what to automate with n8n.

No affiliate, no SaaS, no product selling — by design (`monetization.enabled_paths`). Each action shows **ease** (how easy to
rank / go viral there, comparable across languages: competing videos per daily
searcher), **value** (money path × what a visitor is worth in that market),
what is still unverified, and optional Gemini title ideas + outline.

Each item also carries **research** mined for it:

- **Questions people type** (Google Autocomplete, in that language and
  country): every question is one article, video or short.
- **1-star complaints** about the top Play Store apps (APP / TOOLSITE items):
  the feature list of the better app.
- **Proven viral formats**: small channels whose videos keep getting many
  times their subscriber count, with their titles and a one-line formula.
- **Shorts competition** measured separately from long-form competition.

Every item also shows **💵 roughly $X–$Y per month** once it matures and
the hours it takes; the plan is ranked by that. Measured Google numbers
(DataForSEO, top 20 a day, hard budget) replace guesses for blogs / tool
sites, topics are grouped into **niches** (one channel / site with N related
topics), every item gets a ready **pack for each other open language**, and
**competitor** moves (a big channel entering, an upload surge, a new Google
leader) and **TikTok / YouTube trends** show up on the items they affect.

Publish something → `ode track <url>`: the engine checks it daily against
**your own normal** and says 🔥 WINNER ("isi par 5 aur banao", put on top of
the plan with the next angles) or 🛑 DROP (stop spending time). `ode week`
turns your hours into the week's best mix of work.

You close the loop: `ode done <id>` when you start an item (it never comes
back), `ode result <id> --views … --revenue …` when it earns. The engine then
learns what a visitor really pays in each market and which platform earns
you the most per hour, and re-ranks the plan. Everything also works from the
**phone dashboard** and goes to **n8n** as JSON every morning.

## Always on

`docker compose up -d scheduler web` starts a **scheduler** service that never stops
(restarts itself after crashes and reboots) and runs, in UTC:

| When | Job |
|---|---|
| 04:17 daily | full cycle: discover → collect → score → niches → research miners → DataForSEO → Gemini briefs → language packs |
| 05:57 daily | **your content**: stats + WINNER / DROP verdicts (Telegram on change) |
| 06:37 daily | `reports/plan-latest.md`, `gems-latest.md`, `gems-latest.csv` on the host |
| 06:47 daily | Telegram: **today's plan** (top actions, one line each) |
| 06:52 daily | production pack → your **n8n** webhook + `reports/pack-latest.json` |
| Monday 06:57 | **this week's plan** for your hours (Telegram + `reports/week-latest.txt`) |
| 15:47 daily | **competitor watch** on your topics (+ top plan topics) |
| every 6 h | **TikTok** trending hashtags / sounds + **YouTube** most popular per country |
| weekly | **backtest**: did past top-20s really grow? learns composite weights |
| every hour | Wikipedia **hourly** dumps → same-day spikes → alert |
| every hour | Google Trends "trending now" in every market's country → alert |
| every 6 h | breakout alert (7 days vs prior 7 > 3x, little competition) |
| 16:17 daily | cheap Wikipedia refresh |

Alerts go to Telegram (set `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`) and are
always logged (`ode alerts`); the same alert never repeats within its
cooldown. **Seasonal** topics (a yearly peak month, e.g. exams, tax, Ramadan,
holidays) are put on the plan 2–8 weeks *before* the peak so AI content has
time to rank. 16 markets by default incl. **Urdu (Pakistan)**, Arabic,
Korean and Vietnamese; any `<lang>-<REGION>` can be added. Set
`HTTPS_PROXY` in `.env` to route all traffic through a proxy / VPN gateway.

## Status ✅ harvest · discovery · scoring · action plan · realtime · research miners · learning · backtest · dashboard · n8n

| Area | What exists |
|---|---|
| **Discovery** | Finds topics nobody configured: each language's daily top-1000 Wikipedia articles (mainstream breakouts) + "morelike" neighbours of every seed (niche / tutorial territory). People, films, sports, elections, list pages etc. are filtered out via Wikidata classes. |
| **Cross-language identity** | Every topic is a Wikidata entity: *Heat pump* (en) and *Wärmepumpe* (de) are **one** topic with per-market metrics. Every discovered topic is automatically tracked in every other market. `topics.wiki_langs` records which languages have an article at all (a gap signal on its own). |
| **Collectors** | Wikipedia pageviews (demand backbone, human traffic only), Hacker News, YouTube, Reddit. One uniform interface. |
| **Correct data** | Incremental fetching with overlap; interior zero-days filled; HN counts are exact per day (no 1000-hit truncation); Reddit only writes fully-covered days; "unanswered" only judged after 48h; each market gets its own Reddit scope and localized queries (no English noise in de-DE). |
| **Resilience** | Per-item failure isolation, retries only for transient errors, Retry-After on 429, circuit breaker per host, persisted YouTube quota with least-recently-searched rotation, advisory lock against overlapping cron jobs, orphaned runs closed. |
| **Security** | DB bound to `127.0.0.1` with a password from `.env`; API keys redacted from every error/log; container runs as non-root; dashboard only via HTTPS / SSH tunnel; backups encrypted before leaving the VPS. |
| **Storage** | Postgres 16 + TimescaleDB hypertable, compression after 90 days, bulk COPY upserts, nightly `pg_dump` backups. |
| **Ops** | `ode doctor` checks config, DB and every live API in seconds; every run recorded in `collector_runs` as ok / partial / skipped / error. |
| **Scoring** | Momentum by acceleration (spec model) with spike persistence and a significance test, demand / money / gap / competition, composite, confidence, saturation forecast, cross-language gaps. Top discovered topics are queued for a free YouTube check the next day. |
| **Action plan** | `ode plan`: platform + language + why + steps for the best opportunities, spread over markets; cross-language "be first" actions; Play Store gap check; optional Gemini briefs (titles, outline, hours). |
| **Research miners** | Google Autocomplete long-tail questions (question starters per language, a–z for Latin scripts), 1-star Play Store reviews of the top apps, serial-outlier channels → viral formats (+ Gemini formula), YouTube short-video competition. |
| **Learning** | Stable id per plan item; work log (started / published / dropped + views, revenue, hours); done items never return; monetized results recalibrate market values and platform weights (damped by priors). |
| **Backtest** | Real scorer at past cut-offs (no lookahead), precision@20 vs the random base rate on a bias-safe universe; grid-searched composite weights adopted only when clearly better on most cut-offs. Report headers show the measured numbers. |
| **Dashboard** | Phone-first, no JavaScript: plan, item detail with all research, one-tap done/drop, revenue form, work log, gems, alerts. Basic auth + lockout + CSRF + strict headers; HTTPS on your domain via Caddy. |
| **n8n** | Daily production pack (JSON) POSTed to `N8N_WEBHOOK_URL`, HMAC-signed with `N8N_WEBHOOK_SECRET`. |
| **Dollars** | $/month range + setup hours per item (measured Google volume or Wikipedia demand, growth by phase, capture by ease, RPM × calibrated market value; uncertainties combined in log space); pulled toward your own results once you have them. Plan ranked by it. |
| **Weekly plan** | Hours → winners' "5 more" first, upkeep of what you run (capped, DROP excluded), then new work by $/hour with platform / market spread and a fast + evergreen mix; spread over your days. |
| **Your content** | YouTube / Shorts / Play Store / web pages tracked daily (YouTube 1 unit per 50 videos, Play installs, Google position via DataForSEO); verdict vs the median of your own items at the same age. |
| **DataForSEO** | Search volume (head + long tail), keyword difficulty, SERP top 10 (forum / UGC share) for the top 20 blog / tool items a day; persisted daily budget charged before every call. |
| **Niches** | Embeddings from your RunPod endpoint (any OpenAI-compatible `/embeddings`) or Gemini, cached; leader clustering; niches ranked by demand × growth × openness × money. |
| **Competitors** | Big channels uploading on your topics, weekly upload surges, new Google top-3 domains → stored once, alerted, plan warnings. |
| **Social trends** | TikTok Creative Center hashtags / sounds (best effort, proxy-friendly, tolerant parser) + YouTube most popular; rising hashtags matched to topics and alerted; Shorts boost. |
| **Language packs** | Per other open language: real search keyword, 10 titles, 8 questions (Gemini), checked against Google Autocomplete in that country. |
| **Backups** | Nightly dump; AES-256 encrypted off-site copy via rclone (Drive / B2 / R2 / S3); failure alert; tested restore with TimescaleDB hooks. |
| **Output** | `ode gems`: a ranked shortlist with a plain-language *why* for every row (terminal, Markdown, CSV); written daily to `reports/latest.md`. |
| **Tests** | 240+ tests: parsers, HTTP behaviour, config, momentum edge cases (sparse, single spikes, all-zeros, noise robustness), every score component, and integration tests on a real TimescaleDB (discovery → collection → scoring, no-lookahead guarantee). CI on every push. |

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

# 4. Pehla poora run (pehli dafa ~45-60 minute: 12 languages x 13 mahine ki history)
docker compose run --rm app ode daily

# 5. Dekho kya aaya
docker compose run --rm app ode status
docker compose run --rm app ode watchlist

# 6. Hamesha chalne wala engine (scheduler) + phone dashboard — cron ki zaroorat nahi
docker compose up -d scheduler web
docker compose logs -f scheduler          # dekho kya kar raha hai
# backups ke liye sirf ye ek cron line:
(crontab -l 2>/dev/null; echo "7 3 * * * /opt/discovery-engine/scripts/backup.sh >> /opt/discovery-engine/logs/backup.log 2>&1") | crontab -

# 7. Off-site backup (zaroor karo): VPS mar gaya to sara data + jo engine ne seekha
./scripts/backup.sh --setup     # Google Drive / Backblaze B2 / Cloudflare R2 chuno (ek dafa)
nano .env                       # RCLONE_REMOTE=gdrive:ode-backups, BACKUP_PASSPHRASE=<lamba password>
./scripts/backup.sh             # test: "off-site copy: ... (AES-256 encrypted)"
# wapas lana ho:  ./scripts/restore.sh   (list)   ./scripts/restore.sh ode-2026-10-01.dump.enc
```

### Telegram alerts (phone par)

1. Telegram mein **@BotFather** → `/newbot` → token milega.
2. Apne bot ko koi bhi message bhejo.
3. `https://api.telegram.org/bot<TOKEN>/getUpdates` kholo → `"chat":{"id": ...}` = chat id.
4. `.env` mein `TELEGRAM_BOT_TOKEN=...` aur `TELEGRAM_CHAT_ID=...`, phir
   `docker compose up -d scheduler` aur `docker compose run --rm app ode notify-test`.

Subah ka plan, BREAKOUT, "abhi uchal raha hai" (hourly spikes) aur Google
Trends — sab phone par aayenge.

### Phone dashboard (HTTPS, apne domain par)

1. `.env` mein `DASHBOARD_PASSWORD=` (lamba password; user hamesha `ode`).
2. Namecheap → apne domain ka **A record**, e.g. `ode` → VPS ka IP.
3. `.env` mein `DASHBOARD_DOMAIN=ode.yourdomain.com`
4. `./scripts/deploy.sh` (ya `docker compose --profile https up -d caddy`).
   Caddy khud HTTPS certificate le leta hai. Phone par
   `https://ode.yourdomain.com` kholo → plan, “Shuru kiya”, kamai likho.

Domain nahi lagana? `ssh -L 8080:127.0.0.1:8080 root@VPS` aur phone/laptop par
`http://localhost:8080`. Dashboard internet par kabhi bina HTTPS ke nahi khulta.

### n8n (production automation)

`.env` mein `N8N_WEBHOOK_URL=` (n8n ka *Webhook* node, POST) aur
`N8N_WEBHOOK_SECRET=`. Har subah 06:52 par aaj ka poora plan JSON mein aata hai:
har item ka `id`, platform, language, topic, steps, `questions`,
`complaints`, `formats`, `also_in` languages aur Gemini `brief`. n8n mein
header `X-ODE-Signature` (HMAC-SHA256 of body) check karo. Haath se:
`docker compose run --rm app ode export` (sirf dikhata hai) /
`ode export --send` (bhejta hai).

### Optional keys — har ek kya kholti hai

| `.env` | Kya milta hai |
|---|---|
| `DATAFORSEO_LOGIN/PASSWORD` | Blog / tool items par asli Google searches + difficulty (roz top 20, budget `dataforseo.daily_budget_usd`, default $0.75/din) |
| `EMBEDDINGS_URL` + `EMBEDDINGS_API_KEY` | Niches — RunPod serverless (worker-infinity-embedding) ka `…/openai/v1/embeddings`; na ho to Gemini se |
| `GEMINI_API_KEY` | Briefs, language packs, niche names, viral formula, embeddings fallback |
| `HTTPS_PROXY` | TikTok / Google blocks se bachne ke liye tumhari proxy |

### Publish kiya? Engine ko batao

```bash
docker compose run --rm app ode track https://youtu.be/XXXXXXXXXXX --id 602c7fdd
docker compose run --rm app ode track https://mysite.com/heat-pump-cost --id 9a1b2c3d --keyword "heat pump cost"
docker compose run --rm app ode content          # 🔥 WINNER / 📈 / 🌲 / ➖ / 🛑 DROP + agle 5 angles
docker compose run --rm app ode week --hours 25 --save   # is hafte kya karna hai
docker compose run --rm app ode niches           # ek channel / site = poora niche
docker compose run --rm app ode competitors      # kaun aa raha hai tumhare topics par
```

Ye sab dashboard par bhi hai (Hafta, Mera content, Niches).

### Kaam aur kamai ka hisaab (engine isi se seekhta hai)

```bash
docker compose run --rm app ode done 602c7fdd                 # shuru kiya (plan se hat jayega)
docker compose run --rm app ode done 602c7fdd --status dropped
docker compose run --rm app ode result 602c7fdd --views 15000 --revenue 21.40 --hours 6
docker compose run --rm app ode worklog                       # total kamai, $/ghanta, kya seekha
```

`result` mein **ab tak ka total** likho (har dafa naya total). Sirf woh
results seekhne mein gintay hain jin ki kamai > $0 (monetize hone se pehle
ka $0 kisi market ko bura nahi banata). Ye sab dashboard se bhi hota hai.

`ode doctor` mein sab `[OK]` aaye to system sahi chal raha hai.

### Roz kya dekhna hai

```bash
cat /opt/discovery-engine/reports/plan-latest.md     # AAJ KYA KARNA HAI (pehle ye)
docker compose run --rm app ode plan                  # terminal mein, wohi list
cat /opt/discovery-engine/reports/latest.md          # poori gems list
docker compose run --rm app ode gems                  # terminal mein
docker compose run --rm app ode gems --market de-DE --min-confidence 0.5
```

Har gem ke saath **"why"** likha hota hai: kis phase mein hai (🚀 BREAKOUT /
📈 GROWING / ⛰ PEAKING / ➖ STABLE / 📉 DECLINING), roz kitne log dhoond rahe
hain, paisa kis raste se (SPONSOR / APP / ADS), YouTube par kitna
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
- **Backtest** har hafte chalta hai (`ode backtest`): pichhle 30/60/90 din pehle
  ke top-20 mein se kitne sach mein barhe, random ke muqable mein. Report ke
  upar yahi number likha hota hai. ~60 din ki history ke baad pehla result
  aata hai; tab tak header "UNVALIDATED" kehta hai — list ko shortlist samjho.
- YouTube/Reddit ke `[warn]` sirf keys na hone ki wajah se hain.

Naya code aaye to bas `./scripts/deploy.sh` chalao.

---

## Commands

| Command | What it does |
|---|---|
| `ode doctor` | Config + DB + one tiny live request to every API, parsed with the real parsers. Run after every deploy. |
| `ode daily` | Schema → discover → collect → score → miners → briefs, under a lock. What cron runs. |
| `ode gems [--market de-DE] [--format table\|md\|csv] [--limit 30] [--min-confidence 0.5]` | The ranked shortlist with a *why* per row. `--include-unknown` also lists topics too new for a momentum phase. |
| `ode plan [--format table\|md\|json]` | **What to do now**: platform + language + topic + why + first steps. |
| `ode done <id> [--status started\|published\|dropped] [--hours H] [--note …]` | Log that you took a plan item; it never comes back. |
| `ode result <id> --views N --revenue USD [--hours H]` | Totals so far for an item; the engine learns from monetized results. |
| `ode worklog` | Your items, revenue, $/hour and what the engine learned. |
| `ode mine` | Run the research miners now (questions, 1-star reviews, viral formats). |
| `ode track <url> [--id ID] [--platform P] [--keyword K]` | Track something you published (YouTube / Shorts / Play app / web page). |
| `ode content [--refresh]` · `ode untrack <n>` | Your tracked items with verdicts and next angles. |
| `ode week [--hours H] [--save]` | This week's best mix of work for your hours. |
| `ode niches [--refresh]` | Niches: related topics as one channel / site idea. |
| `ode competitors [--refresh]` | Competitor events on your topics. |
| `ode seo` | DataForSEO verify now (budget-capped). |
| `ode langpacks` | Per-language packs now. |
| `ode notify "text"` | Send any Telegram message (backup failures use it). |
| `ode backtest` | Precision@20 of past rankings vs random; learns composite weights. |
| `ode export [--send]` | Today's production pack as JSON / POST it to `N8N_WEBHOOK_URL`. |
| `ode web [--host 0.0.0.0] [--port 8080]` | Phone dashboard (needs `DASHBOARD_PASSWORD`). |
| `ode scheduler` | Always-on engine (what the `scheduler` compose service runs). |
| `ode realtime hourly\|trends\|breakout\|plan\|social` | Run one realtime job now. |
| `ode alerts` | What was alerted (or would have been, without Telegram). |
| `ode notify-test` | Send a Telegram test message. |
| `ode briefs` | Gemini titles / outline / hours for today's top actions (max 5/day, needs `GEMINI_API_KEY`). |
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
| Money | 0–100 | Only `enabled_paths` (default SPONSOR / APP / ADS). ADS always, with a higher RPM for how-to / tech intent (multilingual keywords or teachable Wikidata classes); APP by utility keywords; SPONSOR for `audience: b2b` seeds or professional classes. Blended revenue per visitor, log-scaled. Comparison/review intent ×0.7. No affiliate, SaaS or selling. |
| Gap | 0–100 | Small-channel outlier strength (views/subs), no channel owning > 30% of top videos, share of unanswered Reddit posts. |
| Competition | 1–100 | YouTube videos published on the topic in the last 180 days, log-normalized within the market. |
| Composite | | momentum × weighted geometric mean(demand, money, gap, 101 − competition). Missing gap/competition = neutral 50 (lowers confidence, never zeros the score). |
| Confidence | 0–1 | 40% history length + 60% how many parts rest on real data; UNKNOWN capped at 0.3. |
| Saturation | | Spec formula on monthly YouTube uploads: last 3 months vs months 10–12 ago → OPEN / NARROWING / CLOSING_FAST; UNKNOWN until those months exist (never guessed). |

Every scoring read goes through `MetricReader`, which requires an `as_of` date
and selects only rows with `date <= as_of`, so the backtest reuses the exact
scorer with zero lookahead.

**Backtest:** cut-offs = newest data − 30 days − {0, 30, 60}. A topic "grew"
when its next-30-day mean is ≥ 1.5× the previous 30 days. Only topics that were
already tracked at the cut-off (or came from seeds / "more like this") are
evaluated — topics admitted *because* they later trended would flatter the
scorer. Composite weights (momentum exponent, demand, gap, competition; money
is never tuned on growth) are grid-searched and adopted only when ≥ 5% better
on average and better on most cut-offs.

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
3. **Dashboard** ← done (phone-first, HTTPS via Caddy)
4. **DataForSEO verify layer** ← done (hard budget cap, top 20/day)
5. **Embeddings + niche clustering** ← done (RunPod / Gemini)
6. **Serial outlier miner + format extraction** ← done
7. **Backtest harness + weight optimization** ← done
8. **Alerts, briefs, learning, competitor watch, your-content verdicts, weekly portfolio,
   language packs, social trends, encrypted backups** ← done
9. Next: automatic revenue import (YouTube Analytics / AdSense / AdMob APIs)
