"""Research miners for the plan items: what exactly to make.

    autocomplete   Google's own suggestions for the topic in the item's
                   language and country: the real long-tail questions, each
                   one an article, a video or a short.
    complaints     1-star Play Store reviews of the top apps for the topic:
                   the feature list of a better AI-built app / web tool.
    formats        "serial outlier" channels: small channels whose videos
                   repeatedly get many times their subscriber count. Their
                   titles are a proven, copyable format (+ an optional Gemini
                   one-line formula).

Run inside `ode daily` (after scoring, before briefs) and on demand with
`ode mine`. Each miner refreshes a topic at most every `refresh_days`, is
throttled, and is isolated: a failing miner never affects the others.
"""

from __future__ import annotations

import asyncio
import json
import string
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from typing import Any

import httpx

from ode.collectors.base import make_fetcher
from ode.config import Config, Secrets
from ode.db import Database
from ode.http import CircuitOpenError
from ode.logging import get_logger
from ode.ratelimit import AsyncTokenBucket
from ode.scoring.text import clean_query

log = get_logger(__name__)

SUGGEST = "https://suggestqueries.google.com/complete/search"
# Question starters per language (prefix variants: "how <topic>" ...).
QUESTION_PREFIXES = {
    "en": ["how", "what", "why", "can", "is"],
    "de": ["wie", "was", "warum", "kann"],
    "fr": ["comment", "pourquoi", "quel"],
    "es": ["cómo", "qué", "por qué"],
    "it": ["come", "cosa", "perché"],
    "pt": ["como", "o que", "por que"],
    "nl": ["hoe", "wat", "waarom"],
    "pl": ["jak", "co", "dlaczego"],
    "tr": ["nasıl", "neden"],
    "id": ["cara", "apa", "kenapa"],
    "sv": ["hur", "vad", "varför"],
    "vi": ["cách", "tại sao"],
    "hi": ["कैसे", "क्या"],
    "ur": ["کیسے", "کیا"],
    "ar": ["كيف", "ما هو", "لماذا"],
    "ja": ["やり方", "とは"],
    "ko": ["방법", "뜻"],
}
LATIN = {"en", "de", "fr", "es", "it", "pt", "nl", "pl", "tr", "id", "sv", "vi"}


# ---------------------------------------------------------------- pure helpers
def suggest_seeds(query: str, language: str, *, letters: bool = True) -> list[str]:
    """The prompts sent to Autocomplete for one topic."""
    seeds = [query]
    seeds += [f"{p} {query}" for p in QUESTION_PREFIXES.get(language, [])]
    if letters and language in LATIN:
        seeds += [f"{query} {c}" for c in string.ascii_lowercase]
    return seeds


def parse_suggestions(payload: Any) -> list[str]:
    """client=firefox answers ["query", ["s1", "s2", ...], ...]."""
    if isinstance(payload, list) and len(payload) >= 2 and isinstance(payload[1], list):
        return [str(s).strip() for s in payload[1] if str(s).strip()]
    return []


def rank_suggestions(found: dict[str, str], query: str, limit: int) -> list[tuple[str, str]]:
    """(suggestion, seed): questions first, then longer (more specific) ones."""
    q = query.casefold()
    words = {w for v in QUESTION_PREFIXES.values() for w in v}

    def key(item: tuple[str, str]) -> tuple[int, int, str]:
        s = item[0].casefold()
        is_q = any(s.startswith(w + " ") or s.endswith("?") for w in words)
        return (0 if is_q else 1, -len(s), s)

    items = [(s, seed) for s, seed in found.items() if s.casefold() != q]
    return sorted(items, key=key)[:limit]


def pick_complaints(reviews: Iterable[dict[str, Any]], *, min_chars: int = 30,
                    limit: int = 8) -> list[dict[str, Any]]:
    """Most useful 1-star reviews: long enough to say something, most liked first."""
    good = [r for r in reviews if r.get("reviewId") and len(str(r.get("content") or "")) >= min_chars]
    good.sort(key=lambda r: (-(r.get("thumbsUpCount") or 0), -len(str(r.get("content")))))
    return good[:limit]


SERIAL_OUTLIER_SQL = """
    WITH v AS (
        SELECT v.channel_id, v.topic_id, v.market, v.title, v.views, c.subs,
               v.views::float / NULLIF(c.subs, 0) AS multiple
        FROM videos v JOIN channels c ON c.id = v.channel_id
        WHERE c.subs BETWEEN $3 AND $4 AND v.views IS NOT NULL AND v.title IS NOT NULL
          AND v.published_at > now() - make_interval(days => $5)
    ), hits AS (SELECT * FROM v WHERE multiple >= $1)
    SELECT channel_id,
           count(*)                                       AS outliers,
           avg(multiple)                                  AS avg_multiple,
           mode() WITHIN GROUP (ORDER BY topic_id)        AS topic_id,
           mode() WITHIN GROUP (ORDER BY market)          AS market,
           (array_agg(title ORDER BY views DESC))[1:8]    AS titles
    FROM hits GROUP BY channel_id HAVING count(*) >= $2
    ORDER BY count(*) DESC, avg(multiple) DESC
"""


# ---------------------------------------------------------------- targets
async def _targets(db: Database, config: Config, platforms: set[str] | None,
                   limit: int) -> list[Any]:
    """Unique (topic, market) of today's plan, best first."""
    from ode.scoring.plan import build_plan

    plan = await build_plan(db, config, _today(), save=False)
    seen: set[tuple[int, str]] = set()
    out = []
    for a in plan:
        if a.topic_id is None or (platforms and a.platform not in platforms):
            continue
        if (a.topic_id, a.market) in seen:
            continue
        seen.add((a.topic_id, a.market))
        out.append(a)
    return out[:limit]


def _today() -> date:
    return datetime.now(UTC).date()


def _aware(value: Any) -> datetime | None:
    """The scraper returns naive UTC datetimes."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _fresh(db: Database, table: str, topic_id: int, market: str, days: int) -> bool:
    return bool(await db.pool.fetchval(
        f"SELECT 1 FROM {table} WHERE topic_id = $1 AND market = $2 "  # fixed table names
        "AND fetched_at > now() - make_interval(days => $3) LIMIT 1", topic_id, market, days))


# ---------------------------------------------------------------- miners
async def mine_autocomplete(db: Database, config: Config, client: httpx.AsyncClient
                            ) -> dict[str, Any]:
    cfg = (config.miners.get("autocomplete") or {})
    if not cfg.get("enabled", True):
        return {"status": "skipped", "reason": "miners.autocomplete.enabled = false"}
    fetcher = make_fetcher(config, client, "autocomplete")
    refresh, keep = int(cfg.get("refresh_days", 7)), int(cfg.get("max_per_topic", 40))
    topics = done = errors = 0
    for a in await _targets(db, config, None, int(cfg.get("max_topics", 15))):
        if await _fresh(db, "longtail", a.topic_id, a.market, refresh):
            continue
        market = config.market(a.market)
        query = clean_query(a.topic).casefold()
        found: dict[str, str] = {}
        try:
            for seed in suggest_seeds(query, market.language,
                                      letters=bool(cfg.get("letters", True))):
                payload = await fetcher.get_json(SUGGEST, params={
                    "client": "firefox", "hl": market.language, "gl": market.region.lower(),
                    "ie": "utf-8", "oe": "utf-8", "q": seed})
                for s in parse_suggestions(payload):
                    found.setdefault(s, seed)
        except CircuitOpenError:
            errors += 1
            break
        except Exception as exc:  # noqa: BLE001 - one topic must not stop the rest
            errors += 1
            log.warning("miners.autocomplete_failed", topic=a.topic, error=str(exc)[:200])
            if not found:
                continue
        rows = rank_suggestions(found, query, keep)
        async with db.pool.acquire() as con, con.transaction():
            await con.execute("DELETE FROM longtail WHERE topic_id = $1 AND market = $2",
                              a.topic_id, a.market)
            await con.executemany(
                "INSERT INTO longtail (topic_id, market, suggestion, seed) VALUES ($1,$2,$3,$4) "
                "ON CONFLICT DO NOTHING",
                [(a.topic_id, a.market, s, seed) for s, seed in rows])
        topics += 1
        done += len(rows)
    return {"status": "error" if errors and not topics else "ok", "topics": topics,
            "suggestions": done, "errors": errors}


def _play_fns() -> tuple[Callable[..., Any], Callable[..., Any]]:
    from google_play_scraper import Sort, reviews, search

    def one_star(app_id: str, *, lang: str, country: str, count: int) -> list[dict[str, Any]]:
        got, _ = reviews(app_id, lang=lang, country=country, sort=Sort.MOST_RELEVANT,
                         count=count, filter_score_with=1)
        return got

    return search, one_star


async def mine_complaints(db: Database, config: Config, *, search_fn: Callable | None = None,
                          reviews_fn: Callable | None = None) -> dict[str, Any]:
    cfg = (config.miners.get("complaints") or {})
    if not cfg.get("enabled", True):
        return {"status": "skipped", "reason": "miners.complaints.enabled = false"}
    if search_fn is None or reviews_fn is None:
        search_fn, reviews_fn = _play_fns()
    bucket = AsyncTokenBucket.from_limit(config.rate_limit("playstore"))
    refresh = int(cfg.get("refresh_days", 14))
    top_apps, per_app = int(cfg.get("top_apps", 3)), int(cfg.get("per_app", 6))
    topics = stored = errors = 0
    for a in await _targets(db, config, {"APP", "TOOLSITE"}, int(cfg.get("max_topics", 8))):
        if await _fresh(db, "app_complaints", a.topic_id, a.market, refresh):
            continue
        market = config.market(a.market)
        lang, country = market.language, market.region.lower()
        try:
            await bucket.acquire()
            results = await asyncio.to_thread(search_fn, clean_query(a.topic), n_hits=10,
                                              lang=lang, country=country)
            rows = []
            for r in [r for r in results or [] if r.get("appId")][:top_apps]:
                await bucket.acquire()
                revs = await asyncio.to_thread(reviews_fn, r["appId"], lang=lang,
                                               country=country, count=60)
                rows += [(a.topic_id, a.market, r["appId"], str(x["reviewId"]),
                          str(x["content"]).strip()[:1000], _aware(x.get("at")))
                         for x in pick_complaints(revs or [], limit=per_app)]
        except Exception as exc:  # noqa: BLE001 - Play markup changes must not break the run
            errors += 1
            log.warning("miners.complaints_failed", topic=a.topic, error=str(exc)[:200])
            continue
        async with db.pool.acquire() as con, con.transaction():
            await con.execute("DELETE FROM app_complaints WHERE topic_id = $1 AND market = $2",
                              a.topic_id, a.market)
            await con.executemany(
                "INSERT INTO app_complaints (topic_id, market, app_id, review_id, content, at) "
                "VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING",
                rows)
        topics += 1
        stored += len(rows)
    return {"status": "error" if errors and not topics else "ok", "topics": topics,
            "complaints": stored, "errors": errors}


FORMULA_PROMPT = """These YouTube titles come from ONE small channel whose videos repeatedly
get many times more views than it has subscribers:

{titles}

In ONE line (max 30 words), in English, describe the repeatable format: the
title formula (use [X] for the variable part) and the video structure.
Return only that line."""


async def mine_formats(db: Database, config: Config, secrets: Secrets | None = None,
                       client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    cfg = (config.miners.get("formats") or {})
    if not cfg.get("enabled", True):
        return {"status": "skipped", "reason": "miners.formats.enabled = false"}
    rows = await db.pool.fetch(
        SERIAL_OUTLIER_SQL, float(cfg.get("min_multiple", 5.0)), int(cfg.get("min_outliers", 2)),
        int(cfg.get("min_subs", 100)), int(cfg.get("max_subs", 200_000)),
        int(cfg.get("within_days", 365)))
    async with db.pool.acquire() as con, con.transaction():
        await con.execute("UPDATE channels SET is_serial_outlier = (id = ANY($1))",
                          [r["channel_id"] for r in rows])
        for r in rows:
            await con.execute(
                """
                INSERT INTO formats (channel_id, topic_id, market, outlier_count, avg_multiple,
                                     titles, mined_at)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, now())
                ON CONFLICT (channel_id) DO UPDATE SET topic_id = EXCLUDED.topic_id,
                    market = EXCLUDED.market, outlier_count = EXCLUDED.outlier_count,
                    avg_multiple = EXCLUDED.avg_multiple, mined_at = now(),
                    formula = CASE WHEN formats.titles = EXCLUDED.titles THEN formats.formula END,
                    titles = EXCLUDED.titles
                """,
                r["channel_id"], r["topic_id"], r["market"], r["outliers"],
                round(r["avg_multiple"], 1), json.dumps(list(r["titles"]), ensure_ascii=False))
    formulas = 0
    if secrets and secrets.gemini_api_key and client is not None:
        formulas = await _formulas(db, config, secrets, client, int(cfg.get("max_formulas", 5)))
    return {"status": "ok", "channels": len(rows), "formulas": formulas}


async def _formulas(db: Database, config: Config, secrets: Secrets, client: httpx.AsyncClient,
                    limit: int) -> int:
    from ode.briefs import API

    todo = await db.pool.fetch("SELECT channel_id, titles FROM formats WHERE formula IS NULL "
                               "ORDER BY outlier_count DESC, avg_multiple DESC LIMIT $1", limit)
    fetcher = make_fetcher(config, client, "gemini")
    url = API.format(model=config.gemini.get("model", "gemini-2.5-flash"))
    made = 0
    for r in todo:
        titles = r["titles"] if isinstance(r["titles"], list) else json.loads(r["titles"])
        prompt = FORMULA_PROMPT.format(titles="\n".join(f"- {t}" for t in titles))
        try:
            payload = await fetcher.request_json(
                "POST", url, headers={"x-goog-api-key": secrets.gemini_api_key},
                json_body={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                           "generationConfig": {"temperature": 0.3}})
            text = "".join(p.get("text", "") for p in
                           payload["candidates"][0]["content"]["parts"]).strip()
        except Exception as exc:  # noqa: BLE001 - formulas are a bonus
            log.warning("miners.formula_failed", error=str(exc)[:200])
            if fetcher.circuit_open:
                break
            continue
        if text:
            await db.pool.execute("UPDATE formats SET formula = $2 WHERE channel_id = $1",
                                  r["channel_id"], text.splitlines()[0][:300])
            made += 1
    return made


async def run_miners(db: Database, config: Config, secrets: Secrets,
                     client: httpx.AsyncClient) -> dict[str, Any]:
    """All miners; each isolated and recorded in collector_runs."""
    jobs = {
        "autocomplete": lambda: mine_autocomplete(db, config, client),
        "complaints": lambda: mine_complaints(db, config),
        "formats": lambda: mine_formats(db, config, secrets, client),
    }
    out: dict[str, Any] = {}
    for name, fn in jobs.items():
        run_id = await db.start_run(f"miner_{name}", None)
        try:
            r = await fn()
        except Exception as exc:  # one miner must not stop the others
            log.exception("miners.failed", miner=name, error=str(exc)[:300])
            r = {"status": "error", "error": str(exc)[:200]}
        items = next((v for k, v in r.items() if k in ("suggestions", "complaints", "channels")),
                     0)
        await db.finish_run(run_id, status=r["status"] if r["status"] in
                            ("ok", "error", "skipped") else "ok",
                            items=int(items or 0), errors=int(r.get("errors", 0) or 0),
                            detail=r)
        out[name] = r
    return out


