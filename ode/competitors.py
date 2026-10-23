"""Competitor watch: is the window on your topics closing?

For the topics you work on (work log, tracked content) plus the top plan
topics, once a day:

    big_channel    a channel with >= `big_subs` subscribers uploaded a video
                   on the topic in the last 7 days (YouTube search by date)
    upload_surge   uploads in the last 7 days >= `surge_ratio` x the normal
                   week (from the monthly upload counts already collected)
    serp_newcomer  a new domain entered Google's top 3 (from the DataForSEO
                   snapshots already taken; no extra cost)

Each event is stored once (competitor_events) and sent to Telegram. Plan
items on that topic and market then carry a warning and a small ease
penalty: move fast, or take a narrower angle.

Cost: ~102 YouTube units per topic (search + videos + channels), capped by
`max_topics` and the shared daily YouTube quota.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from ode.collectors.base import make_fetcher
from ode.collectors.youtube import CHANNELS, SEARCH, VIDEOS, parse_total_results
from ode.config import Config, Secrets
from ode.db import Database, PgQuotaStore
from ode.logging import get_logger
from ode.models import RawSignal
from ode.quota import QuotaTracker
from ode.scoring.actions import LANGUAGE_NAME
from ode.scoring.text import clean_query

log = get_logger(__name__)


def _today() -> date:
    return datetime.now(UTC).date()


def big_channel_events(videos: list[dict[str, Any]], channels: dict[str, dict[str, Any]],
                       big_subs: int) -> list[dict[str, Any]]:
    """Pure: new uploads by big channels (one event per channel, its best video)."""
    best: dict[str, dict[str, Any]] = {}
    for v in videos:
        ch = channels.get(v.get("channel_id") or "")
        if not ch or (ch.get("subs") or 0) < big_subs:
            continue
        cur = best.get(v["channel_id"])
        if cur is None or (v.get("views") or 0) > (cur.get("views") or 0):
            best[v["channel_id"]] = {**v, "channel": ch.get("title"), "subs": ch.get("subs")}
    return sorted(best.values(), key=lambda x: -(x.get("subs") or 0))


def is_surge(week_new: int | None, monthly: list[float], *, ratio: float, min_new: int
             ) -> tuple[bool, float | None]:
    """Pure: this week's uploads vs a normal week (mean of recent months / 4.35)."""
    if week_new is None or not monthly:
        return False, None
    normal = sum(monthly) / len(monthly) / 4.35
    if normal < 1:
        return week_new >= max(min_new, 3 * ratio), None
    mult = week_new / normal
    return (week_new >= min_new and mult >= ratio), round(mult, 1)


def serp_newcomers(latest: list[str], previous: list[str], top: int = 3) -> list[str]:
    """Pure: domains in the latest top-N that were not in the previous top 10."""
    return [d for d in latest[:top] if d and d not in previous[:10]]


async def _targets(db: Database, config: Config) -> list[dict[str, Any]]:
    from ode.scoring.plan import build_plan

    c = config.competitors
    rows = await db.pool.fetch(
        """
        SELECT DISTINCT topic_id, market FROM (
            SELECT topic_id, market FROM work_log WHERE status IN ('started', 'published')
            UNION SELECT topic_id, market FROM my_content WHERE active) x
        WHERE topic_id IS NOT NULL AND market IS NOT NULL
        """)
    keys = [(r["topic_id"], r["market"]) for r in rows]
    plan = await build_plan(db, config, _today(), save=False)
    for a in plan[: int(c.get("top_plan", 5))]:
        if a.topic_id is not None:
            keys.append((a.topic_id, a.market))
    out, seen = [], set()
    for tid, market in keys:
        if (tid, market) in seen or market not in config.market_codes:
            continue
        seen.add((tid, market))
        row = await db.pool.fetchrow(
            """
            SELECT t.topic_key, t.canonical_name,
                   (SELECT w.term FROM watchlist w WHERE w.topic_key = t.topic_key
                      AND w.market = $2 LIMIT 1) AS term
            FROM topics t WHERE t.id = $1
            """, tid, market)
        if row:
            out.append({"topic_id": tid, "market": market, "topic_key": row["topic_key"],
                        "name": row["term"] or row["canonical_name"]})
    return out[: int(c.get("max_topics", 10))]


async def _record(db: Database, key: str, tid: int, market: str, kind: str,
                  detail: dict[str, Any]) -> bool:
    status = await db.pool.execute(
        "INSERT INTO competitor_events (key, topic_id, market, kind, detail) "
        "VALUES ($1, $2, $3, $4, $5::jsonb) ON CONFLICT (key) DO NOTHING",
        key, tid, market, kind, json.dumps(detail, ensure_ascii=False, default=str))
    return status.endswith(" 1")


async def watch_job(config: Config, secrets: Secrets, db: Database, client: httpx.AsyncClient
                    ) -> dict[str, Any]:
    from ode.notify import Notifier, html_escape

    c = config.competitors
    if not c.get("enabled", True):
        return {"status": "skipped", "reason": "competitors.enabled = false"}
    targets = await _targets(db, config)
    if not targets:
        return {"status": "skipped", "reason": "nothing to watch yet"}
    today = _today()
    notifier = Notifier(config, secrets, client, db)
    big_subs = int(c.get("big_subs", 250_000))
    stats = {"topics": 0, "events": 0, "alerts": 0, "errors": 0}
    fetcher = make_fetcher(config, client, "youtube")
    quota = QuotaTracker(PgQuotaStore(db), config.quotas)
    cost = config.youtube_unit_cost
    signals: list[RawSignal] = []
    for t in targets:
        market = config.market(t["market"])
        lang = LANGUAGE_NAME.get(market.language, market.language)
        events: list[tuple[str, str, dict[str, Any], str]] = []
        # --- YouTube: who uploaded on this topic in the last 7 days?
        if secrets.youtube_api_key and await quota.check_and_reserve("youtube",
                                                                      cost["search"] + 2):
            try:
                after = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
                search = await fetcher.get_json(SEARCH, params={
                    "key": secrets.youtube_api_key, "q": clean_query(t["name"]), "part": "id",
                    "type": "video", "order": "date", "maxResults": 25, "publishedAfter": after,
                    "regionCode": market.region, "relevanceLanguage": market.language})
                week_new = parse_total_results(search)
                ids = [it["id"]["videoId"] for it in (search or {}).get("items", [])
                       if (it.get("id") or {}).get("videoId")]
                videos, channels = [], {}
                if ids:
                    vp = await fetcher.get_json(VIDEOS, params={
                        "key": secrets.youtube_api_key, "part": "snippet,statistics",
                        "id": ",".join(ids)})
                    for v in (vp or {}).get("items", []):
                        sn, st = v.get("snippet") or {}, v.get("statistics") or {}
                        videos.append({"id": v.get("id"), "title": sn.get("title"),
                                       "channel_id": sn.get("channelId"),
                                       "views": int(st.get("viewCount") or 0)})
                    ch_ids = sorted({v["channel_id"] for v in videos if v["channel_id"]})
                    if ch_ids:
                        cp = await fetcher.get_json(CHANNELS, params={
                            "key": secrets.youtube_api_key, "part": "snippet,statistics",
                            "id": ",".join(ch_ids)})
                        channels = {ch["id"]: {"title": (ch.get("snippet") or {}).get("title"),
                                               "subs": int((ch.get("statistics") or {}).get(
                                                   "subscriberCount") or 0)}
                                    for ch in (cp or {}).get("items", [])}
                for ev in big_channel_events(videos, channels, big_subs)[:3]:
                    events.append((
                        f"big:{t['topic_id']}:{t['market']}:{ev['channel_id']}", "big_channel", ev,
                        (f"⚠️ <b>Bara channel aa gaya</b> ({lang}): "
                         f"<b>{html_escape(t['name'])}</b>\n{html_escape(ev['channel'] or '')} "
                         f"({ev['subs']:,} subs) ne video daali: "
                         f"“{html_escape(ev['title'] or '')}”\nKhirki band ho rahi hai — "
                         "jaldi publish karo ya narrow angle lo.")))
                if week_new is not None:
                    signals.append(RawSignal("youtube", t["market"], t["name"],
                                             "youtube_weekly_uploads", float(week_new), today,
                                             topic_key=t["topic_key"]))
                    monthly = [r["value"] for r in await db.pool.fetch(
                        "SELECT value FROM topic_metrics WHERE topic_id = $1 AND market = $2 "
                        "AND source = 'youtube' AND metric_name = 'youtube_monthly_uploads' "
                        "ORDER BY date DESC LIMIT 3", t["topic_id"], t["market"])]
                    surge, mult = is_surge(week_new, monthly,
                                           ratio=float(c.get("surge_ratio", 3.0)),
                                           min_new=int(c.get("surge_min_new", 10)))
                    if surge:
                        iso = today.isocalendar()
                        events.append((
                            f"surge:{t['topic_id']}:{t['market']}:{iso.year}w{iso.week}",
                            "upload_surge", {"week_new": week_new, "multiple": mult},
                            f"⚠️ <b>Competition tezi se barh raha</b> ({lang}): "
                            f"<b>{html_escape(t['name'])}</b> — is hafte {week_new:,} nayi videos"
                            + (f" (normal se {mult:g}x)" if mult else "")
                            + ". Jo karna hai abhi karo."))
            except Exception as exc:  # noqa: BLE001 - one topic must not stop the rest
                stats["errors"] += 1
                log.warning("competitors.youtube_failed", topic=t["name"], error=str(exc)[:200])
        # --- Google: a new domain in the top 3 (from stored DataForSEO snapshots)
        snaps = await db.pool.fetch(
            """
            SELECT fetched_at, array_agg(domain ORDER BY position) AS domains
            FROM serp_results WHERE topic_id = $1 AND market = $2
            GROUP BY fetched_at ORDER BY fetched_at DESC LIMIT 2
            """, t["topic_id"], t["market"])
        if len(snaps) == 2:
            for dom in serp_newcomers(list(snaps[0]["domains"]), list(snaps[1]["domains"])):
                events.append((
                    f"serp:{t['topic_id']}:{t['market']}:{dom}", "serp_newcomer",
                    {"domain": dom, "at": snaps[0]["fetched_at"]},
                    (f"⚠️ <b>Google top 3 mein naya site</b> ({lang}): "
                     f"<b>{html_escape(t['name'])}</b> — {html_escape(dom)}")))
        for key, kind, detail, text in events:
            if await _record(db, key, t["topic_id"], t["market"], kind, detail):
                stats["events"] += 1
                if await notifier.send_once(key, "competitor", text,
                                            cooldown=timedelta(days=14)):
                    stats["alerts"] += 1
        stats["topics"] += 1
    if signals:
        await db.upsert_signals(signals)
    status = "error" if stats["errors"] and not stats["topics"] else "ok"
    return {"status": status, **stats}


async def recent_events(db: Database, days: int = 14) -> dict[tuple[int, str], list[dict]]:
    rows = await db.pool.fetch(
        "SELECT topic_id, market, kind, detail, seen_at FROM competitor_events "
        "WHERE seen_at > now() - make_interval(days => $1) ORDER BY seen_at DESC", days)
    out: dict[tuple[int, str], list[dict]] = {}
    for r in rows:
        d = r["detail"] if isinstance(r["detail"], dict) else json.loads(r["detail"] or "{}")
        out.setdefault((r["topic_id"], r["market"]), []).append({"kind": r["kind"], **d})
    return out
