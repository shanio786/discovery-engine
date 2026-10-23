"""Social trends: what is taking off on TikTok and YouTube right now.

    TikTok Creative Center   trending hashtags and sounds per country (public
                             pages; the data sits in the page's __NEXT_DATA__
                             JSON). Best effort: TikTok changes its pages and
                             may block datacenter IPs — set HTTPS_PROXY to
                             your proxy. The parser looks for the fields by
                             name anywhere in the JSON, so small layout
                             changes do not break it; `ode doctor` says
                             clearly when it does.
    YouTube most popular     videos.list chart=mostPopular per country
                             (1 quota unit per country): reliable.

Hashtags are matched to a Wikipedia article in the market's language and
go through the normal discovery filters (no people / films / sports unless
enabled), so a rising hashtag becomes a tracked topic. Rising hashtags
(big rank jump, or new in the top 20) are alerted. Plan items whose topic
is trending there get a Shorts boost and say so.

Instagram has no public trend source; it is not guessed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx

from ode.collectors.base import make_fetcher
from ode.collectors.youtube import VIDEOS
from ode.config import Config, Secrets
from ode.db import Database, PgQuotaStore
from ode.logging import get_logger
from ode.quota import QuotaTracker

log = get_logger(__name__)

TIKTOK_URLS = {
    "hashtag": ("https://ads.tiktok.com/business/creativecenter/inspiration/popular/hashtag/"
                "pc/en?countryCode={cc}&period=7"),
    "sound": ("https://ads.tiktok.com/business/creativecenter/inspiration/popular/music/"
              "pc/en?countryCode={cc}&period=7"),
}
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like "
              "Gecko) Chrome/126.0 Safari/537.36")
STOP_TAGS = {"fyp", "foryou", "foryoupage", "fy", "fypシ", "viral", "trending", "tiktok",
             "xyzbca", "capcut", "duet", "explore", "love", "funny", "trend", "foryourpage",
             "viralvideo", "tiktoktrend", "fyppppppppppppppppppppppp", "parati", "pourtoi",
             "fürdich", "keşfet", "explorepage", "reels", "shorts", "meme", "memes"}
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def _num(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def next_data(html: str) -> Any:
    m = _NEXT_DATA.search(html or "")
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def parse_hashtags(data: Any) -> list[dict[str, Any]]:
    """Pure: every object carrying a hashtag name, wherever it sits in the JSON."""
    out: dict[str, dict[str, Any]] = {}
    for d in _walk(data):
        name = _first(d, "hashtagName", "hashtag_name")
        if not isinstance(name, str) or not name.strip():
            continue
        key = name.strip().lstrip("#").casefold()
        rank = _num(_first(d, "rank", "ranking"))
        item = {"name": key, "rank": int(rank) if rank else None,
                "posts": _num(_first(d, "publishCnt", "publish_cnt", "videoCount", "postCount")),
                "views": _num(_first(d, "videoViews", "video_views", "viewCount")),
                "rank_change": _num(_first(d, "rankDiff", "rank_diff")),
                "is_new": bool(_first(d, "isNew", "is_new") or
                               _first(d, "rankDiffType", "rank_diff_type") == 4)}
        if key not in out or (item["rank"] or 999) < (out[key]["rank"] or 999):
            out[key] = item
    return sorted(out.values(), key=lambda x: x["rank"] or 999)


def parse_sounds(data: Any) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for d in _walk(data):
        title = d.get("title")
        author = _first(d, "author", "artist", "authorName")
        if not isinstance(title, str) or not isinstance(author, str):
            continue
        if _first(d, "clipId", "clip_id", "songId", "song_id", "musicId", "rank") is None:
            continue
        key = f"{title.strip()} — {author.strip()}"
        rank = _num(d.get("rank"))
        out.setdefault(key, {"name": key, "rank": int(rank) if rank else None,
                             "rank_change": _num(_first(d, "rankDiff", "rank_diff")),
                             "link": d.get("link") or d.get("url")})
    return sorted(out.values(), key=lambda x: x["rank"] or 999)


def rising(items: list[dict[str, Any]], previous: dict[str, int | None], *, rise_min: int,
           top_new: int) -> list[dict[str, Any]]:
    """Pure: hashtags that jumped `rise_min` ranks, or entered the top `top_new`."""
    out = []
    for it in items:
        if it["name"] in STOP_TAGS or len(it["name"]) < 3:
            continue
        rank = it.get("rank") or 999
        before = previous.get(it["name"])
        jump = (before - rank) if before else (it.get("rank_change") or 0)
        new_top = it["name"] not in previous and rank <= top_new and bool(previous)
        if jump >= rise_min or new_top or (it.get("is_new") and rank <= top_new):
            out.append({**it, "jump": int(jump) if jump else None})
    return out


async def fetch_tiktok(client: httpx.AsyncClient, url: str) -> Any:
    resp = await client.get(url, headers={"User-Agent": BROWSER_UA,
                                          "Accept-Language": "en-US,en;q=0.9"}, timeout=30.0)
    if resp.status_code >= 400:
        raise RuntimeError(f"TikTok Creative Center answered HTTP {resp.status_code}")
    data = next_data(resp.text)
    if data is None:
        raise RuntimeError("no __NEXT_DATA__ in the page (layout changed or blocked)")
    return data


async def _upsert(db: Database, source: str, market: str, kind: str,
                  items: list[dict[str, Any]], metric_key: str) -> dict[str, int | None]:
    """Store items; returns the PREVIOUS ranks (name -> rank) seen in the last 3 days."""
    prev = {r["name"]: r["rank"] for r in await db.pool.fetch(
        "SELECT name, rank FROM social_trends WHERE source = $1 AND market = $2 AND kind = $3 "
        "AND last_seen > now() - interval '3 days'", source, market, kind)}
    await db.pool.executemany(
        """
        INSERT INTO social_trends (source, market, kind, name, rank, metric, rank_change, detail)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
        ON CONFLICT (source, market, kind, name) DO UPDATE SET rank = EXCLUDED.rank,
            metric = EXCLUDED.metric, rank_change = EXCLUDED.rank_change,
            detail = EXCLUDED.detail, last_seen = now()
        """,
        [(source, market, kind, it["name"][:300], it.get("rank"), it.get(metric_key),
          int(it["rank_change"]) if it.get("rank_change") is not None else None,
          json.dumps({k: v for k, v in it.items() if k not in ("name",)}, ensure_ascii=False,
                     default=str)) for it in items])
    return prev


async def social_job(config: Config, secrets: Secrets, db: Database, client: httpx.AsyncClient
                     ) -> dict[str, Any]:
    from ode.discovery import Discoverer
    from ode.notify import Notifier, html_escape
    from ode.realtime.trends import wiki_match

    cfg = config.social
    stats = {"tiktok_tags": 0, "tiktok_sounds": 0, "youtube_videos": 0, "matched": 0,
             "alerts": 0, "errors": 0}
    notifier = Notifier(config, secrets, client, db)
    disc = Discoverer(config, client, db)
    wiki = make_fetcher(config, client, "wikipedia")
    tt = cfg.get("tiktok") or {}
    yt = cfg.get("youtube") or {}
    seen_regions: set[str] = set()
    tt_bucket = make_fetcher(config, client, "tiktok").bucket
    quota = QuotaTracker(PgQuotaStore(db), config.quotas)
    for market in config.markets():
        if market.region in seen_regions:
            continue
        seen_regions.add(market.region)
        # --- TikTok (best effort)
        if tt.get("enabled", True):
            urls = {**TIKTOK_URLS, **(tt.get("urls") or {})}
            try:
                await tt_bucket.acquire()
                data = await fetch_tiktok(client, urls["hashtag"].format(cc=market.region))
                tags = parse_hashtags(data)[: int(tt.get("keep", 50))]
                prev = await _upsert(db, "tiktok", market.code, "hashtag", tags, "posts")
                stats["tiktok_tags"] += len(tags)
                up = rising(tags, prev, rise_min=int(tt.get("rise_min", 20)),
                            top_new=int(tt.get("top_new", 20)))
                for it in up[: int(tt.get("max_alerts_per_run", 3))]:
                    title = None
                    try:
                        title = await wiki_match(wiki, market, it["name"])
                    except Exception:  # noqa: BLE001 - a match is optional
                        title = None
                    active = None
                    if title:
                        res = await disc.admit_titles(market, [title], "trend")
                        active = next((r for r in res if r["active"]), None)
                    if active:
                        stats["matched"] += 1
                        await db.pool.execute(
                            "UPDATE social_trends SET wiki_title = $3, topic_id = (SELECT id "
                            "FROM topics WHERE topic_key = $4) WHERE source = 'tiktok' AND "
                            "market = $1 AND kind = 'hashtag' AND name = $2",
                            market.code, it["name"], active["canonical"], active["topic_key"])
                    elif title:
                        continue  # matched an excluded topic (person, film …): no alert
                    jump = f", rank ↑{it['jump']}" if it.get("jump") else ""
                    posts = f"{it['posts']:,.0f} posts, " if it.get("posts") else ""
                    text = (f"📈 <b>TikTok par uth raha</b> ({market.region}): "
                            f"<b>#{html_escape(it['name'])}</b> — {posts}rank {it.get('rank')}"
                            f"{jump}\nAI short / Reel abhi banao, trend ke shuru mein.")
                    if await notifier.send_once(f"tiktok:{market.region}:{it['name']}", "tiktok",
                                                text, cooldown=timedelta(days=3)):
                        stats["alerts"] += 1
                if tt.get("sounds", True):
                    await tt_bucket.acquire()
                    sdata = await fetch_tiktok(client, urls["sound"].format(cc=market.region))
                    sounds = parse_sounds(sdata)[: int(tt.get("keep", 50))]
                    await _upsert(db, "tiktok", market.code, "sound", sounds, "rank")
                    stats["tiktok_sounds"] += len(sounds)
            except Exception as exc:  # noqa: BLE001 - TikTok must never break the rest
                stats["errors"] += 1
                log.warning("social.tiktok_failed", market=market.code, error=str(exc)[:200])
        # --- YouTube most popular (1 unit per country)
        if yt.get("enabled", True) and secrets.youtube_api_key and \
                await quota.check_and_reserve("youtube", 1):
            try:
                payload = await make_fetcher(config, client, "youtube").get_json(VIDEOS, params={
                    "key": secrets.youtube_api_key, "part": "snippet,statistics",
                    "chart": "mostPopular", "regionCode": market.region,
                    "maxResults": int(yt.get("max_results", 50))})
                vids = [{"name": (v.get("snippet") or {}).get("title", "")[:300], "rank": i + 1,
                         "views": _num((v.get("statistics") or {}).get("viewCount")),
                         "video_id": v.get("id"),
                         "channel": (v.get("snippet") or {}).get("channelTitle")}
                        for i, v in enumerate((payload or {}).get("items", []))]
                vids = [v for v in vids if v["name"]]
                await _upsert(db, "youtube", market.code, "video", vids, "views")
                stats["youtube_videos"] += len(vids)
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                log.warning("social.youtube_failed", market=market.code, error=str(exc)[:200])
    await db.pool.execute("DELETE FROM social_trends WHERE last_seen < now() - interval '30 days'")
    got = stats["tiktok_tags"] + stats["youtube_videos"]
    status = "error" if stats["errors"] and not got else ("partial" if stats["errors"] else "ok")
    return {"status": status, **stats}


async def topic_signals(db: Database, names: dict[tuple[int, str], str]
                        ) -> dict[tuple[int, str], list[str]]:
    """(topic_id, market) -> lines like "TikTok par #x trending (rank 4)"."""
    out: dict[tuple[int, str], list[str]] = {}
    rows = await db.pool.fetch(
        "SELECT source, market, kind, name, rank, topic_id FROM social_trends "
        "WHERE last_seen > now() - interval '3 days'")
    by_market_titles: dict[str, list[tuple[str, int | None]]] = {}
    for r in rows:
        if r["source"] == "tiktok" and r["topic_id"] is not None:
            out.setdefault((r["topic_id"], r["market"]), []).append(
                f"TikTok par #{r['name']} trending (rank {r['rank']})")
        elif r["source"] == "youtube":
            by_market_titles.setdefault(r["market"], []).append((r["name"].casefold(), r["rank"]))
    for (tid, market), name in names.items():
        n = (name or "").casefold()
        if len(n) < 4:
            continue
        hits = [rank for title, rank in by_market_titles.get(market, [])
                if re.search(rf"(?<!\w){re.escape(n)}(?!\w)", title)]
        if hits:
            out.setdefault((tid, market), []).append(
                f"YouTube trending mein {len(hits)} video (best rank {min(hits)})")
    return out
