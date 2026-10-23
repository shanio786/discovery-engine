"""`ode doctor` — one command that proves the deployment works end to end.

Checks config, database (schema, hypertable, compression) and makes ONE tiny
live request to every upstream API, parsing the answer with the same parsers
the collectors use. So a changed API format or a blocked host shows up here,
in seconds, instead of as a silent gap in the data a week later.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ode.collectors.base import make_fetcher as _make_fetcher
from ode.collectors.hackernews import SEARCH as HN_SEARCH
from ode.collectors.hackernews import day_bounds, parse_nbhits
from ode.collectors.wikipedia import REST_BASE, parse_pageviews
from ode.config import Config, Secrets
from ode.db import Database, PgQuotaStore
from ode.pipeline import http_client
from ode.quota import QuotaTracker
from ode.wikimeta import WikiMeta


def make_fetcher(config: Config, client, source: str):
    """Doctor answers fast: one retry, short backoff."""
    f = _make_fetcher(config, client, source)
    f.max_retries, f.backoff_base = 2, 0.5
    return f


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


async def _guard(name: str, fn: Callable[[], Awaitable[str]], required: bool = True) -> Check:
    try:
        return Check(name, True, await fn(), required)
    except Exception as exc:  # noqa: BLE001 - doctor reports, never crashes
        return Check(name, False, f"{type(exc).__name__}: {str(exc)[:200]}", required)


async def run_doctor(config: Config, secrets: Secrets) -> list[Check]:
    checks: list[Check] = [Check("config", True, f"{len(config.seeds)} seeds, markets "
                                 f"{', '.join(config.market_codes)}")]
    db: Database | None = None

    async def db_check() -> str:
        nonlocal db
        db = await Database.connect(secrets.database_url, max_size=2)
        await db.init_schema()
        tables = await db.pool.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ANY($1)",
            ["topics", "topic_metrics", "watchlist", "collector_runs", "api_quota"],
        )
        if tables != 5:
            raise RuntimeError(f"expected 5 core tables, found {tables}")
        return "connected, schema applied"

    checks.append(await _guard("database", db_check))
    if db is not None:
        async def hyper() -> str:
            n = await db.pool.fetchval(
                "SELECT count(*) FROM timescaledb_information.hypertables "
                "WHERE hypertable_name = 'topic_metrics'"
            )
            if not n:
                raise RuntimeError("topic_metrics is a plain table (TimescaleDB missing)")
            comp = await db.pool.fetchval(
                "SELECT count(*) FROM timescaledb_information.jobs "
                "WHERE proc_name = 'policy_compression' AND hypertable_name = 'topic_metrics'"
            )
            return "hypertable ok" + (", compression policy ok" if comp else ", NO compression")

        checks.append(await _guard("timescaledb", hyper, required=False))

    yesterday = datetime.now(UTC).date() - timedelta(days=2)
    async with http_client(config) as client:
        wiki = make_fetcher(config, client, "wikipedia")
        meta = WikiMeta(wiki, make_fetcher(config, client, "wikidata"))

        async def pageviews() -> str:
            start = yesterday - timedelta(days=6)
            url = (f"{REST_BASE}/en.wikipedia/all-access/user/Raspberry_Pi/daily/"
                   f"{start:%Y%m%d}/{yesterday:%Y%m%d}")
            sigs = parse_pageviews(await wiki.get_json(url), "en-US", "Raspberry_Pi")
            if not sigs:
                raise RuntimeError("no pageview rows parsed")
            return f"{len(sigs)} days parsed, last={sigs[-1].value:.0f} views"

        async def top() -> str:
            for back in range(4):
                arts = await meta.top_articles("en.wikipedia", yesterday - timedelta(days=back))
                if arts:
                    return f"{len(arts)} top articles parsed"
            raise RuntimeError("no top-articles day available in the last 5 days")

        qid_holder: dict[str, Any] = {}

        async def pageprops() -> str:
            pages = await meta.resolve_titles("en.wikipedia.org", ["Raspberry Pi"])
            page = pages.get("Raspberry Pi")
            if not page or not page.qid:
                raise RuntimeError("could not resolve 'Raspberry Pi' to a Wikidata id")
            qid_holder["qid"] = page.qid
            return f"Raspberry Pi -> {page.qid}"

        async def wikidata() -> str:
            qid = qid_holder.get("qid") or "Q42"  # Douglas Adams: a stable, well-known id
            ents = await meta.entities([qid])
            ent = ents.get(qid)
            if not ent or not ent.sitelinks:
                raise RuntimeError("entity/sitelinks not parsed")
            return f"{qid}: {len(ent.wiki_langs)} languages, dewiki={ent.sitelinks.get('dewiki')!r}"

        async def morelike() -> str:
            titles = await meta.morelike("en.wikipedia.org", "Raspberry Pi", 5)
            if not titles:
                raise RuntimeError("no morelike results parsed")
            return f"{len(titles)} similar: {', '.join(titles[:3])}"

        async def hn() -> str:
            t0, t1 = day_bounds(yesterday)
            n = parse_nbhits(await make_fetcher(config, client, "hackernews").get_json(
                HN_SEARCH, params={"query": "Python", "tags": "story", "hitsPerPage": 0,
                                   "numericFilters": f"created_at_i>={t0},created_at_i<{t1}"}))
            if n is None:
                raise RuntimeError("nbHits not parsed")
            return f"{n} 'Python' stories on {yesterday}"

        for name, fn in [("wikimedia pageviews", pageviews), ("wikimedia top", top),
                         ("wikipedia pageprops", pageprops), ("wikidata entities", wikidata),
                         ("wikipedia morelike", morelike), ("hacker news", hn)]:
            checks.append(await _guard(name, fn))

        if secrets.youtube_api_key:
            async def youtube() -> str:
                quota = QuotaTracker(PgQuotaStore(db), config.quotas) if db else None
                if quota and not await quota.check_and_reserve("youtube", 1):
                    raise RuntimeError("daily quota exhausted")
                payload = await make_fetcher(config, client, "youtube").get_json(
                    "https://www.googleapis.com/youtube/v3/videos",
                    params={"key": secrets.youtube_api_key, "part": "id",
                            "chart": "mostPopular", "maxResults": 1, "regionCode": "US"})
                if not (payload or {}).get("items"):
                    raise RuntimeError("no items — key invalid or API not enabled?")
                return "key valid (1 quota unit used)"
            checks.append(await _guard("youtube", youtube, required=False))
        else:
            checks.append(Check("youtube", False, "YOUTUBE_API_KEY not set (collector will skip)",
                                required=False))

        if secrets.reddit_client_id and secrets.reddit_client_secret:
            async def reddit() -> str:
                payload = await make_fetcher(config, client, "reddit").request_json(
                    "POST", "https://www.reddit.com/api/v1/access_token",
                    data={"grant_type": "client_credentials"},
                    auth=(secrets.reddit_client_id or "", secrets.reddit_client_secret or ""),
                    headers={"User-Agent": secrets.reddit_user_agent})
                if not (payload or {}).get("access_token"):
                    raise RuntimeError("no access_token — check client id/secret")
                return "OAuth token ok"
            checks.append(await _guard("reddit", reddit, required=False))
        else:
            checks.append(Check("reddit", False, "REDDIT_CLIENT_ID/SECRET not set "
                                "(collector will skip)", required=False))

        async def playstore() -> str:
            import asyncio

            from ode.collectors.playstore import _default_search, parse_installs
            results = await asyncio.to_thread(_default_search(), "habit tracker", n_hits=5,
                                              lang="en", country="us")
            if not results or not results[0].get("appId"):
                raise RuntimeError("no apps parsed — Play markup may have changed")
            top = results[0]
            return (f"{len(results)} apps; top: {top.get('title')!r} "
                    f"{parse_installs(top.get('installs')) or '?'} installs")

        checks.append(await _guard("play store", playstore, required=False))

        async def hourly_dump() -> str:
            from ode.realtime.hourly import dump_url
            hour = (datetime.now(UTC) - timedelta(hours=3)).replace(minute=0, second=0,
                                                                     microsecond=0)
            r = await client.head(dump_url(hour), timeout=20.0)
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code} for the {hour:%H}:00 dump")
            size = int(r.headers.get("content-length", 0)) // 1_000_000
            return f"hourly dump {hour:%Y-%m-%d %H}:00 available ({size} MB)"

        checks.append(await _guard("wikipedia hourly", hourly_dump, required=False))

        async def trends() -> str:
            from ode.realtime.trends import DEFAULT_URL, fetch_trends
            items = await fetch_trends(make_fetcher(config, client, "trends"),
                                       config.market("en-US"),
                                       (config.realtime.get("trends") or {}).get("url",
                                                                                DEFAULT_URL))
            if not items:
                raise RuntimeError("no trend items parsed (feed URL/format changed?)")
            return f"{len(items)} US trends; top: {items[0].title!r}"

        checks.append(await _guard("google trends", trends, required=False))

        async def autocomplete() -> str:
            from ode.miners import SUGGEST, parse_suggestions
            got = parse_suggestions(await make_fetcher(config, client, "autocomplete").get_json(
                SUGGEST, params={"client": "firefox", "hl": "en", "gl": "us",
                                 "ie": "utf-8", "oe": "utf-8", "q": "how to"}))
            if not got:
                raise RuntimeError("no suggestions parsed (blocked? set HTTPS_PROXY)")
            return f"{len(got)} suggestions, e.g. {got[0]!r}"

        checks.append(await _guard("google autocomplete", autocomplete, required=False))

        if secrets.telegram_bot_token:
            async def telegram() -> str:
                from ode.notify import TG_API
                me = await make_fetcher(config, client, "telegram").get_json(
                    TG_API.format(token=secrets.telegram_bot_token, method="getMe"))
                if not (me or {}).get("ok"):
                    raise RuntimeError("getMe failed — token wrong?")
                chat = "chat id set" if secrets.telegram_chat_id else "TELEGRAM_CHAT_ID missing"
                return f"bot @{me['result'].get('username')} ({chat})"
            checks.append(await _guard("telegram", telegram, required=False))
        else:
            checks.append(Check("telegram", False, "TELEGRAM_BOT_TOKEN not set (alerts only "
                                "logged, see `ode alerts`)", required=False))

        if secrets.gemini_api_key:
            async def gemini() -> str:
                from ode.briefs import API
                payload = await make_fetcher(config, client, "gemini").request_json(
                    "POST", API.format(model=config.gemini.get("model", "gemini-2.5-flash")),
                    headers={"x-goog-api-key": secrets.gemini_api_key},
                    json_body={"contents": [{"role": "user", "parts": [{"text": "Reply: ok"}]}]})
                if not (payload or {}).get("candidates"):
                    raise RuntimeError("no candidates — key/model wrong?")
                return f"model {config.gemini.get('model', 'gemini-2.5-flash')} answers"
            checks.append(await _guard("gemini", gemini, required=False))
        else:
            checks.append(Check("gemini", False, "GEMINI_API_KEY not set (briefs will skip)",
                                required=False))

    checks.append(Check("n8n webhook", bool(secrets.n8n_webhook_url),
                        "set (pack POSTed daily, signed)" if secrets.n8n_webhook_url and
                        secrets.n8n_webhook_secret else "set (unsigned: add N8N_WEBHOOK_SECRET)"
                        if secrets.n8n_webhook_url else
                        "N8N_WEBHOOK_URL not set (pack only written to reports/)",
                        required=False))
    checks.append(Check("dashboard", bool(secrets.dashboard_password),
                        "password set" if secrets.dashboard_password else
                        "DASHBOARD_PASSWORD not set (dashboard answers 503)", required=False))
    if db is not None:
        await db.close()
    return checks
