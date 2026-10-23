"""Your published content: daily stats, verdicts, and "make 5 more" items.

    ode track <url> [--id <plan item>]     YouTube video / short, Play Store app,
                                           or a blog / tool page (+ --keyword)

Every day (scheduler job `content`) the engine fetches each item's numbers
(YouTube views / likes / comments: 1 quota unit per 50 videos; Play Store
installs and rating; Google position of a page's keyword via DataForSEO,
weekly, within its budget) and judges it against YOUR OWN normal at the
same age (the median of your other items of that platform at that age;
defaults until you have three):

    🔥 WINNER     >= 2x your normal          -> "isi par 5 aur banao" (top of the plan)
    📈 PROMISING  1-2x
    🌲 EVERGREEN  old but still growing every week
    ➖ FLAT       below normal
    🛑 DROP       old, < 0.3x normal and stalled -> stop spending time on it
    ⏳ TOO_EARLY  too young to judge

A WINNER becomes a plan item with the topic's unused long-tail questions
and related topics from its niche; WINNER / DROP changes go to Telegram.
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from ode.collectors.base import make_fetcher
from ode.collectors.youtube import VIDEOS
from ode.config import Config, Secrets
from ode.db import Database, PgQuotaStore
from ode.logging import get_logger
from ode.quota import QuotaTracker
from ode.ratelimit import AsyncTokenBucket

log = get_logger(__name__)

PLATFORMS = ("YOUTUBE", "SHORTS", "BLOG", "TOOLSITE", "APP")
ICON = {"WINNER": "🔥", "PROMISING": "📈", "EVERGREEN": "🌲", "FLAT": "➖", "DROP": "🛑",
        "TOO_EARLY": "⏳", "UNKNOWN": "❔"}
DEFAULTS: dict[str, Any] = {
    "min_age_days": {"YOUTUBE": 3, "SHORTS": 2, "APP": 7, "BLOG": 14, "TOOLSITE": 14},
    "drop_age_days": {"YOUTUBE": 21, "SHORTS": 10, "APP": 45, "BLOG": 75, "TOOLSITE": 75},
    # what a new item usually has after 7 days when you have no history yet
    "default_7d": {"YOUTUBE": 300, "SHORTS": 1500, "APP": 30, "BLOG": 100, "TOOLSITE": 100},
    "winner_ratio": 2.0,
    "drop_ratio": 0.3,
    "min_history_items": 3,
    "serp_every_days": 7,
    "serp_depth": 50,
    "double_down_max": 3,
}
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class ContentError(ValueError):
    pass


def _today() -> date:
    return datetime.now(UTC).date()


def parse_url(url: str) -> tuple[str, str]:
    """(kind, external id): YOUTUBE / SHORTS + video id, APP + package, WEB + url."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ContentError(f"not a URL: {url!r}")
    host = parts.netloc.lower().removeprefix("www.").removeprefix("m.")
    path = parts.path.rstrip("/")
    vid = None
    kind = "YOUTUBE"
    if host == "youtu.be":
        vid = path.lstrip("/").split("/")[0]
    elif host.endswith("youtube.com"):
        if path.startswith("/shorts/"):
            vid, kind = path.split("/")[2], "SHORTS"
        elif path.startswith("/live/"):
            vid = path.split("/")[2]
        else:
            vid = (parse_qs(parts.query).get("v") or [""])[0]
    if vid is not None:
        if not _YT_ID.match(vid or ""):
            raise ContentError(f"no YouTube video id in {url!r}")
        return kind, vid
    if host == "play.google.com":
        pkg = (parse_qs(parts.query).get("id") or [""])[0]
        if not pkg:
            raise ContentError(f"no app id in {url!r}")
        return "APP", pkg
    return "WEB", url.strip()


# ---------------------------------------------------------------- verdicts
@dataclass(frozen=True)
class Verdict:
    kind: str
    text: str
    ratio: float | None = None
    value: float | None = None
    expected: float | None = None
    age_days: int = 0


def _cfg(cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for k, v in (cfg or {}).items():
        out[k] = {**out.get(k, {}), **v} if isinstance(v, dict) else v
    return out


def value_at_age(history: Sequence[tuple[date, float]], published: date, age: int
                 ) -> float | None:
    """Cumulative metric at `age` days (linear between snapshots). None if the
    history does not reach that age."""
    pts = sorted(((d - published).days, v) for d, v in history if v is not None)
    if not pts or pts[-1][0] < age:
        return None
    prev = (0, 0.0)
    for a, v in pts:
        if a >= age:
            if a == prev[0]:
                return v
            return prev[1] + (v - prev[1]) * (age - prev[0]) / (a - prev[0])
        prev = (a, v)
    return pts[-1][1]


def expected_at_age(platform: str, age: int, others: Sequence[tuple[Sequence[tuple[date, float]],
                                                                     date]],
                    cfg: Mapping[str, Any] | None = None) -> tuple[float, int]:
    """Your normal at this age: median of your other items (>= min_history_items),
    else the default curve default_7d x sqrt(age / 7). Returns (value, items used)."""
    c = _cfg(cfg)
    vals = [v for hist, pub in others if (v := value_at_age(hist, pub, age)) is not None]
    if len(vals) >= int(c["min_history_items"]):
        return max(1.0, statistics.median(vals)), len(vals)
    return float(c["default_7d"].get(platform, 100)) * (max(age, 1) / 7) ** 0.5, 0


def judge(platform: str, history: Sequence[tuple[date, float]], published: date, today: date,
          others: Sequence[tuple[Sequence[tuple[date, float]], date]] = (),
          cfg: Mapping[str, Any] | None = None) -> Verdict:
    """Pure: verdict for one item from its cumulative views (installs for apps)."""
    c = _cfg(cfg)
    age = (today - published).days
    pts = sorted((d, v) for d, v in history if v is not None)
    if not pts:
        return Verdict("UNKNOWN", "abhi koi number nahi aaya", age_days=age)
    if age < int(c["min_age_days"].get(platform, 7)):
        return Verdict("TOO_EARLY", f"sirf {age} din purani — abhi faisla jaldi hai",
                       value=pts[-1][1], age_days=age)
    now_v = pts[-1][1]
    week_ago = value_at_age(pts, published, max(0, (pts[-1][0] - published).days - 7))
    gained = now_v - (week_ago or 0.0)
    expected, n = expected_at_age(platform, age, others, c)
    ratio = now_v / expected if expected else None
    unit = "installs" if platform == "APP" else "views"
    base = "tumhara normal" if n else "aam normal"
    if ratio is not None and ratio >= float(c["winner_ratio"]):
        return Verdict("WINNER", f"{now_v:,.0f} {unit} = {base} se {ratio:.1f}x — isi par 5 aur "
                       "banao", ratio, now_v, expected, age)
    if age >= 45 and now_v > 0 and gained >= 0.05 * now_v:
        return Verdict("EVERGREEN", f"{age} din baad bhi har hafte +{gained:,.0f} {unit} — "
                       "isi tarah ka aur content banao", ratio, now_v, expected, age)
    if ratio is not None and ratio >= 1.0:
        return Verdict("PROMISING", f"{now_v:,.0f} {unit} ({ratio:.1f}x {base}) — chal rahi hai",
                       ratio, now_v, expected, age)
    if (age >= int(c["drop_age_days"].get(platform, 30)) and ratio is not None
            and ratio < float(c["drop_ratio"]) and gained < 0.03 * max(now_v, 1)):
        return Verdict("DROP", f"{age} din mein sirf {now_v:,.0f} {unit} ({ratio:.1f}x {base}), "
                       "ruk gayi — is par aur waqt mat lagao", ratio, now_v, expected, age)
    return Verdict("FLAT", f"{now_v:,.0f} {unit} ({(ratio or 0):.1f}x {base})", ratio, now_v,
                   expected, age)


def judge_position(position: int | None, age: int, cfg: Mapping[str, Any] | None = None,
                   platform: str = "BLOG") -> Verdict:
    """Pure: verdict for a page from its Google position (None = not in the checked depth)."""
    c = _cfg(cfg)
    if age < int(c["min_age_days"].get(platform, 14)):
        return Verdict("TOO_EARLY", f"sirf {age} din purana — Google ko waqt do", age_days=age)
    if position is not None and position <= 10:
        return Verdict("WINNER", f"Google page 1 (#{position}) — isi topic par aur pages likho",
                       value=position, age_days=age)
    if position is not None and position <= 20:
        return Verdict("PROMISING", f"Google #{position} — 2-3 internal links aur ek update se "
                       "page 1 mumkin", value=position, age_days=age)
    if age >= int(c["drop_age_days"].get(platform, 75)) and (position is None or position > 50):
        return Verdict("DROP", f"{age} din mein bhi top 50 mein nahi — is keyword ko chhor do",
                       value=position, age_days=age)
    where = f"#{position}" if position else f"top {c['serp_depth']} se bahar"
    return Verdict("FLAT", f"Google {where}", value=position, age_days=age)


# ---------------------------------------------------------------- storage
async def add_content(db: Database, url: str, *, action_id: str | None = None,
                      platform: str | None = None, keyword: str | None = None,
                      market: str | None = None) -> dict[str, Any]:
    from ode import worklog
    from ode.scoring.text import clean_query

    kind, ext = parse_url(url)
    item = None
    if action_id:
        item = await db.pool.fetchrow("SELECT * FROM plan_items WHERE action_id = $1",
                                      action_id)
        if item is None:
            raise worklog.UnknownAction(f"no plan item {action_id!r}")
    plan_platform = item["platform"] if item else None
    if platform:
        platform = platform.upper()
    elif kind in ("YOUTUBE", "SHORTS"):
        platform = "SHORTS" if kind == "SHORTS" or plan_platform == "SHORTS" else "YOUTUBE"
    elif kind == "APP":
        platform = "APP"
    else:
        platform = plan_platform if plan_platform in ("BLOG", "TOOLSITE") else None
    if platform not in PLATFORMS:
        raise ContentError("which platform? use --platform BLOG or TOOLSITE for web pages")
    if platform in ("BLOG", "TOOLSITE") and not keyword and item:
        keyword = clean_query(item["topic"])
    row = await db.pool.fetchrow(
        """
        INSERT INTO my_content (url, platform, external_id, action_id, topic_id, market, keyword)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (url) DO UPDATE SET active = true,
            action_id = COALESCE(EXCLUDED.action_id, my_content.action_id),
            topic_id = COALESCE(EXCLUDED.topic_id, my_content.topic_id),
            market = COALESCE(EXCLUDED.market, my_content.market),
            keyword = COALESCE(EXCLUDED.keyword, my_content.keyword)
        RETURNING *
        """,
        url.strip(), platform, ext if kind != "WEB" else None, action_id,
        item["topic_id"] if item else None, market or (item["market"] if item else None),
        keyword)
    if action_id:
        await worklog.mark(db, action_id, "published")
    return dict(row)


async def untrack(db: Database, content_id: int) -> bool:
    status = await db.pool.execute("UPDATE my_content SET active = false WHERE id = $1",
                                   content_id)
    return status.endswith(" 1")


async def _stats(db: Database, content_id: int, today: date, **values: Any) -> None:
    cols = [k for k, v in values.items() if v is not None]
    if not cols:
        return
    sets = ", ".join(f"{k} = EXCLUDED.{k}" for k in cols)
    await db.pool.execute(
        f"INSERT INTO content_stats (content_id, date, {', '.join(cols)}) "  # fixed names
        f"VALUES ($1, $2, {', '.join(f'${i + 3}' for i in range(len(cols)))}) "
        f"ON CONFLICT (content_id, date) DO UPDATE SET {sets}",
        content_id, today, *[values[k] for k in cols])


# ---------------------------------------------------------------- fetching
def _int(x: Any) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


async def _youtube(db: Database, config: Config, secrets: Secrets, client: httpx.AsyncClient,
                   items: list[dict[str, Any]], today: date) -> int:
    if not secrets.youtube_api_key or not items:
        return 0
    quota = QuotaTracker(PgQuotaStore(db), config.quotas)
    fetcher = make_fetcher(config, client, "youtube")
    done = 0
    for i in range(0, len(items), 50):
        batch = items[i:i + 50]
        if not await quota.check_and_reserve("youtube", 1):
            log.info("content.youtube_quota")
            break
        payload = await fetcher.get_json(VIDEOS, params={
            "key": secrets.youtube_api_key, "part": "snippet,statistics",
            "id": ",".join(it["external_id"] for it in batch)})
        by_id = {v.get("id"): v for v in (payload or {}).get("items", [])}
        for it in batch:
            v = by_id.get(it["external_id"])
            if not v:
                continue
            st, sn = v.get("statistics") or {}, v.get("snippet") or {}
            await _stats(db, it["id"], today, views=_int(st.get("viewCount")),
                         likes=_int(st.get("likeCount")), comments=_int(st.get("commentCount")))
            pub = sn.get("publishedAt")
            await db.pool.execute(
                "UPDATE my_content SET title = COALESCE($2, title), "
                "published_at = COALESCE(published_at, $3::timestamptz) WHERE id = $1",
                it["id"], sn.get("title"),
                datetime.fromisoformat(pub) if pub else None)  # 3.11+ parses "Z"
            done += 1
    return done


def _play_app() -> Callable[..., dict[str, Any]]:
    from google_play_scraper import app

    return app


async def _apps(db: Database, config: Config, items: list[dict[str, Any]], today: date,
                app_fn: Callable[..., dict[str, Any]] | None = None) -> int:
    if not items:
        return 0
    app_fn = app_fn or _play_app()
    bucket = AsyncTokenBucket.from_limit(config.rate_limit("playstore"))
    done = 0
    for it in items:
        m = config.market(it["market"]) if it["market"] in config.market_codes else None
        try:
            await bucket.acquire()
            d = await asyncio.to_thread(app_fn, it["external_id"],
                                        lang=m.language if m else "en",
                                        country=m.region.lower() if m else "us")
        except Exception as exc:  # noqa: BLE001 - one app must not stop the others
            log.warning("content.app_failed", app=it["external_id"], error=str(exc)[:200])
            continue
        await _stats(db, it["id"], today, installs=_int(d.get("realInstalls") or
                                                        d.get("minInstalls")),
                     rating=d.get("score"))
        await db.pool.execute("UPDATE my_content SET title = COALESCE(title, $2) WHERE id = $1",
                              it["id"], d.get("title"))
        done += 1
    return done


async def _web(db: Database, config: Config, secrets: Secrets, client: httpx.AsyncClient,
               items: list[dict[str, Any]], today: date) -> int:
    from ode.dataforseo import BudgetExceeded, DataForSEO, domain_of

    dfs = DataForSEO(config, secrets, client, db)
    c = _cfg(config.content)
    if not dfs.enabled or not items:
        return 0
    done = 0
    for it in items:
        if not it["keyword"] or it["market"] not in config.market_codes:
            continue
        last = await db.pool.fetchval(
            "SELECT max(date) FROM content_stats WHERE content_id = $1 AND position IS NOT NULL",
            it["id"])
        if last and (today - last).days < int(c["serp_every_days"]):
            continue
        try:
            rows, _, err = await dfs.serp(config.market(it["market"]), it["keyword"],
                                          depth=int(c["serp_depth"]))
        except BudgetExceeded:
            break
        if err:
            continue
        mine = domain_of(it["url"])
        pos = next((r["position"] for r in rows if r["domain"] == mine), None)
        # not found = one past the checked depth, so "dropped out" is visible
        await _stats(db, it["id"], today, position=pos or int(c["serp_depth"]) + 1)
        done += 1
    return done


# ---------------------------------------------------------------- the job
async def _history(db: Database, ids: list[int], metric: str
                   ) -> dict[int, list[tuple[date, float]]]:
    rows = await db.pool.fetch(
        f"SELECT content_id, date, {metric} AS v FROM content_stats "  # fixed column names
        f"WHERE content_id = ANY($1) AND {metric} IS NOT NULL ORDER BY date", ids)
    out: dict[int, list[tuple[date, float]]] = {}
    for r in rows:
        out.setdefault(r["content_id"], []).append((r["date"], float(r["v"])))
    return out


async def suggestions(db: Database, item: Mapping[str, Any], n: int = 5) -> list[str]:
    """Unused long-tail questions of the topic + related topics from its niche."""
    if item.get("topic_id") is None:
        return []
    used = {r["title"].casefold() for r in await db.pool.fetch(
        "SELECT title FROM my_content WHERE topic_id = $1 AND title IS NOT NULL",
        item["topic_id"])}
    qs = [r["suggestion"] for r in await db.pool.fetch(
        "SELECT suggestion FROM longtail WHERE topic_id = $1 AND market = $2 "
        "ORDER BY volume DESC NULLS LAST, length(suggestion) DESC LIMIT 30",
        item["topic_id"], item.get("market") or "")]
    out = [q for q in qs if not any(q.casefold() in u for u in used)][:n]
    if len(out) < n:
        sib = await db.pool.fetch(
            """
            SELECT t.canonical_name FROM topics t
            JOIN topics me ON me.id = $1 AND me.cluster_id IS NOT NULL
                          AND t.cluster_id = me.cluster_id AND t.id <> me.id
            ORDER BY t.id LIMIT $2
            """, item["topic_id"], n - len(out))
        out += [f"related: {r['canonical_name']}" for r in sib]
    return out


async def judge_all(db: Database, config: Config, today: date) -> list[dict[str, Any]]:
    """Recompute verdicts for every active item; returns items whose verdict changed."""
    c = _cfg(config.content)
    items = [dict(r) for r in await db.pool.fetch("SELECT * FROM my_content WHERE active")]
    if not items:
        return []
    ids = [it["id"] for it in items]
    views = await _history(db, ids, "views")
    installs = await _history(db, ids, "installs")
    positions = await _history(db, ids, "position")
    changed = []
    for it in items:
        pub = (it["published_at"] or it["added_at"]).date()
        p = it["platform"]
        if p in ("BLOG", "TOOLSITE") and positions.get(it["id"]):
            v = judge_position(int(positions[it["id"]][-1][1]), (today - pub).days, c, p)
            if v.value is not None and v.value > int(c["serp_depth"]):
                v = Verdict(v.kind, v.text, v.ratio, None, v.expected, v.age_days)
        else:
            series = installs if p == "APP" else views
            others = [(series[o["id"]], (o["published_at"] or o["added_at"]).date())
                      for o in items if o["id"] != it["id"] and o["platform"] == p
                      and series.get(o["id"])]
            v = judge(p, series.get(it["id"], []), pub, today, others, c)
        detail = {"text": v.text, "ratio": v.ratio, "value": v.value, "expected": v.expected,
                  "age_days": v.age_days}
        if v.kind in ("WINNER", "EVERGREEN"):
            detail["suggestions"] = await suggestions(db, it)
        if v.kind != it["verdict"]:
            changed.append({**it, "verdict": v.kind, "detail": detail})
        await db.pool.execute(
            "UPDATE my_content SET verdict = $2, verdict_detail = $3::jsonb, "
            "verdict_at = CASE WHEN verdict IS DISTINCT FROM $2 THEN now() ELSE verdict_at END "
            "WHERE id = $1", it["id"], v.kind, json.dumps(detail, ensure_ascii=False))
    return changed


async def content_job(config: Config, secrets: Secrets, db: Database,
                      client: httpx.AsyncClient, *, app_fn=None) -> dict[str, Any]:
    from ode.notify import Notifier, html_escape

    today = _today()
    items = [dict(r) for r in await db.pool.fetch("SELECT * FROM my_content WHERE active")]
    if not items:
        return {"status": "skipped", "reason": "nothing tracked — `ode track <url>`"}
    out = {"youtube": 0, "apps": 0, "web": 0}
    try:
        out["youtube"] = await _youtube(db, config, secrets, client,
                                        [i for i in items if i["platform"] in
                                         ("YOUTUBE", "SHORTS") and i["external_id"]], today)
    except Exception as exc:  # noqa: BLE001 - one source must not stop the others
        log.warning("content.youtube_failed", error=str(exc)[:200])
    out["apps"] = await _apps(db, config, [i for i in items if i["platform"] == "APP"], today,
                              app_fn)
    try:
        out["web"] = await _web(db, config, secrets, client,
                                [i for i in items if i["platform"] in ("BLOG", "TOOLSITE")],
                                today)
    except Exception as exc:  # noqa: BLE001
        log.warning("content.web_failed", error=str(exc)[:200])
    changed = await judge_all(db, config, today)
    notifier = Notifier(config, secrets, client, db)
    alerts = 0
    for it in changed:
        if it["verdict"] not in ("WINNER", "DROP"):
            continue
        name = it["title"] or it["url"]
        extra = ""
        if it["verdict"] == "WINNER" and it["detail"].get("suggestions"):
            extra = "\nAgle 5:\n" + "\n".join(f"• {html_escape(s)}"
                                              for s in it["detail"]["suggestions"][:5])
        text = (f"{ICON[it['verdict']]} <b>{it['verdict']}</b>: {html_escape(name)}\n"
                f"{html_escape(it['detail']['text'])}{extra}\n{it['url']}")
        if await notifier.send_once(f"content:{it['id']}:{it['verdict']}", "content", text,
                                    cooldown=timedelta(days=60)):
            alerts += 1
    return {"status": "ok", **out, "changed": len(changed), "alerts": alerts}


async def double_down_actions(db: Database, config: Config, today: date) -> list[Any]:
    """WINNER content -> top-of-plan items: make more of exactly what works."""
    from ode.scoring.actions import Action
    from ode.scoring.dollars import DEFAULTS as DOLLAR_DEFAULTS
    from ode.scoring.dollars import Dollars

    c = _cfg(config.content)
    rows = await db.pool.fetch(
        "SELECT * FROM my_content WHERE active AND verdict IN ('WINNER', 'EVERGREEN') "
        "AND topic_id IS NOT NULL ORDER BY (verdict = 'WINNER') DESC, "
        "(verdict_detail->>'ratio')::float DESC NULLS LAST LIMIT $1",
        int(c["double_down_max"]))
    week = f"{today.isocalendar().year}w{today.isocalendar().week}"
    out = []
    for r in rows:
        d = r["verdict_detail"] if isinstance(r["verdict_detail"], dict) else json.loads(
            r["verdict_detail"] or "{}")
        topic = await db.pool.fetchval("SELECT canonical_name FROM topics WHERE id = $1",
                                       r["topic_id"])
        market = r["market"] or "en-US"
        language = market.split("-")[0]
        sugg = d.get("suggestions") or []
        name = r["title"] or r["url"]
        steps = [("Bilkul isi format, length aur andaaz mein 5 aur banao — yehi chal raha hai: "
                  f"“{name}”.")]
        if sugg:
            steps.append("In angles par: " + "; ".join(sugg[:5]) + ".")
        steps += ["Title / thumbnail pattern wohi rakho, sirf topic badlo.",
                  "Pehle 48 ghante dekho: jo sab se tez chale, us par series / long version."]
        value = float(d.get("value") or 0)
        a = Action(r["platform"], market, language, topic or name, 999.0, 90.0, 80.0,
                   [f"{ICON[r['verdict']]} tumhara content chal raha hai: {d.get('text', '')}",
                    "jo chal raha hai usi par double karna sab se pakki kamai hai"],
                   steps, [], r["topic_id"], variant=f"more:{r['id']}:{week}")
        if value and r["platform"] in ("YOUTUBE", "SHORTS"):
            rpm = (3.0 if r["platform"] == "YOUTUBE" else 0.06) * config.market_value(market)
            monthly = value / max(1.0, d.get("age_days") or 30) * 30 * rpm / 1000 * 5 * 0.5
            hours = float({**DOLLAR_DEFAULTS["effort_hours"],
                           **(config.dollars.get("effort_hours") or {})}[r["platform"]])
            a.dollars = Dollars(round(monthly * 0.3, 2), round(monthly * 1.5, 2),
                                hours * 5 * 0.6,  # 5 more, batched: cheaper than new ones
                                "tumhare content ke asli views se")
        out.append(a)
    return out
