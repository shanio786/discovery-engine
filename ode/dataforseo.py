"""DataForSEO verify layer: REAL Google numbers for the best blog / tool items.

For the top plan items per day (BLOG / TOOLSITE, at most `max_topics`,
default 20) in their own market and language:

    search volume      Google Ads monthly searches for the main keyword and
                       its long-tail questions (one request per market)
    keyword difficulty DataForSEO Labs 0-100 (one request per market)
    SERP top 10        who ranks now; forums / Q&A / user content in the top
                       10 means a good article can get in

Stored as topic_metrics (source `dataforseo`: gsv, gsv_longtail, kd, cpc,
serp_weak_share), long-tail volumes on `longtail.volume`, the SERP in
`serp_results`. The scorer and plan then show measured difficulty instead
of a guess.

Money safety: every call is charged BEFORE it is made against a persisted
daily budget (`dataforseo.daily_budget_usd`, in 1/10000 USD units in
`api_quota`), using a configured cost estimate; the real cost the API
reports is booked afterwards. A topic is re-checked at most every
`refresh_days`. Without DATAFORSEO_LOGIN / _PASSWORD it skips cleanly.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from ode.collectors.base import make_fetcher
from ode.config import Config, Secrets
from ode.db import Database, PgQuotaStore
from ode.logging import get_logger
from ode.models import Market, RawSignal
from ode.quota import QuotaTracker
from ode.scoring.text import clean_query

log = get_logger(__name__)

API = "https://api.dataforseo.com/v3"
VOLUME = f"{API}/keywords_data/google_ads/search_volume/live"
DIFFICULTY = f"{API}/dataforseo_labs/google/bulk_keyword_difficulty/live"
SERP = f"{API}/serp/google/organic/live/regular"
USER = f"{API}/appendix/user_data"
UNIT = 10_000  # budget ledger unit: 1/10000 USD

LOCATIONS = {
    "US": "United States", "GB": "United Kingdom", "DE": "Germany", "JP": "Japan",
    "FR": "France", "ES": "Spain", "IT": "Italy", "BR": "Brazil", "NL": "Netherlands",
    "PL": "Poland", "TR": "Turkey", "ID": "Indonesia", "IN": "India", "PK": "Pakistan",
    "SA": "Saudi Arabia", "KR": "South Korea", "VN": "Vietnam", "SE": "Sweden",
    "BD": "Bangladesh", "TH": "Thailand", "RU": "Russia", "UA": "Ukraine", "IR": "Iran",
    "MY": "Malaysia", "RO": "Romania", "CZ": "Czechia", "GR": "Greece", "HU": "Hungary",
    "FI": "Finland", "DK": "Denmark", "NO": "Norway",
}
# Results from these sites in the top 10 = the query is not owned by strong pages.
WEAK_DOMAINS = ("reddit.com", "quora.com", "pinterest.", "facebook.com", "tiktok.com",
                "medium.com", "stackexchange.com", "stackoverflow.com", "tumblr.com",
                "blogspot.", "wordpress.com", "answers.", "forum", "community.",
                "linkedin.com/pulse", "twitter.com", "x.com", "instagram.com", "brainly.")
_BAD_CHARS = re.compile(r"[,!@%^()={};~`<>?\\|\"*#$&+\[\]]")


class BudgetExceeded(RuntimeError):
    pass


def sanitize_keyword(text: str) -> str | None:
    """Google Ads accepts <= 80 chars, <= 10 words and no symbols."""
    kw = _BAD_CHARS.sub(" ", text.casefold())
    kw = " ".join(kw.split()[:10])[:80].strip()
    return kw or None


def domain_of(url_or_domain: str | None) -> str:
    if not url_or_domain:
        return ""
    host = urlsplit(url_or_domain).netloc if "//" in url_or_domain else url_or_domain
    return host.lower().removeprefix("www.")


def is_weak(domain: str, url: str | None = None) -> bool:
    text = f"{domain} {url or ''}".lower()
    return any(w in text for w in WEAK_DOMAINS)


def _task_results(payload: Any) -> tuple[list[dict[str, Any]], float, str | None]:
    """(results, reported cost, error) from a DataForSEO response."""
    if not isinstance(payload, dict):
        return [], 0.0, "empty response"
    cost = float(payload.get("cost") or 0.0)
    tasks = payload.get("tasks") or []
    if payload.get("status_code") != 20000 or not tasks:
        return [], cost, f"{payload.get('status_code')} {payload.get('status_message')}"
    task = tasks[0]
    if task.get("status_code") != 20000:
        return [], cost, f"{task.get('status_code')} {task.get('status_message')}"
    return [r for r in task.get("result") or [] if isinstance(r, dict)], cost, None


def parse_volumes(payload: Any) -> tuple[dict[str, dict[str, Any]], float, str | None]:
    results, cost, err = _task_results(payload)
    out = {str(r.get("keyword", "")).casefold(): {
        "volume": r.get("search_volume"), "cpc": r.get("cpc"),
        "competition_index": r.get("competition_index")} for r in results if r.get("keyword")}
    return out, cost, err


def parse_difficulty(payload: Any) -> tuple[dict[str, float], float, str | None]:
    results, cost, err = _task_results(payload)
    out: dict[str, float] = {}
    for r in results:
        for item in r.get("items") or []:
            if item.get("keyword") and item.get("keyword_difficulty") is not None:
                out[str(item["keyword"]).casefold()] = float(item["keyword_difficulty"])
    return out, cost, err


def parse_serp(payload: Any) -> tuple[list[dict[str, Any]], float, str | None]:
    results, cost, err = _task_results(payload)
    rows = []
    for r in results:
        for item in r.get("items") or []:
            if item.get("type") != "organic":
                continue
            dom = domain_of(item.get("domain") or item.get("url"))
            rows.append({"position": int(item.get("rank_group") or len(rows) + 1),
                         "domain": dom, "url": item.get("url"), "title": item.get("title"),
                         "weak": is_weak(dom, item.get("url"))})
    rows.sort(key=lambda x: x["position"])
    return rows, cost, err


class DataForSEO:
    """Budget-guarded client. Every method charges an estimate first."""

    def __init__(self, config: Config, secrets: Secrets, client: httpx.AsyncClient,
                 db: Database) -> None:
        self.cfg = config.dataforseo
        self.auth = (secrets.dataforseo_login or "", secrets.dataforseo_password or "")
        self.http = make_fetcher(config, client, "dataforseo")
        budget = int(float(self.cfg.get("daily_budget_usd", 0.75)) * UNIT)
        self.quota = QuotaTracker(PgQuotaStore(db), {"dataforseo": budget})
        self.est = {"volume": 0.075, "difficulty": 0.03, "serp": 0.003,
                    **(self.cfg.get("est_cost_usd") or {})}
        self.spent = 0.0

    @property
    def enabled(self) -> bool:
        return all(self.auth)

    async def _post(self, kind: str, url: str, task: dict[str, Any]) -> Any:
        est = int(float(self.est[kind]) * UNIT)
        if not await self.quota.check_and_reserve("dataforseo", est):
            raise BudgetExceeded(f"daily DataForSEO budget reached (before {kind})")
        payload = await self.http.request_json("POST", url, json_body=[task], auth=self.auth)
        real = int(float((payload or {}).get("cost") or 0.0) * UNIT)
        if real > est:  # book the difference so the ledger matches the real bill
            await self.quota.store.add_used("dataforseo", datetime.now(UTC).date(), real - est,
                                            self.quota.limits["dataforseo"] or 0)
        self.spent += max(real, est) / UNIT
        return payload

    @staticmethod
    def _where(market: Market) -> dict[str, str]:
        return {"location_name": LOCATIONS.get(market.region, market.region),
                "language_code": market.language}

    async def volumes(self, market: Market, keywords: list[str]):
        return parse_volumes(await self._post("volume", VOLUME,
                                              {"keywords": keywords, **self._where(market)}))

    async def difficulty(self, market: Market, keywords: list[str]):
        return parse_difficulty(await self._post("difficulty", DIFFICULTY,
                                                 {"keywords": keywords, **self._where(market)}))

    async def serp(self, market: Market, keyword: str, depth: int = 10):
        return parse_serp(await self._post("serp", SERP, {
            "keyword": keyword, "depth": depth, **self._where(market)}))

    async def balance(self) -> float | None:
        payload = await self.http.request_json("GET", USER, auth=self.auth)
        results, _, err = _task_results(payload)
        if err or not results:
            raise RuntimeError(err or "no user data")
        return (results[0].get("money") or {}).get("balance")


async def _targets(db: Database, config: Config, today: date) -> list[Any]:
    from ode.scoring.plan import build_plan

    cfg = config.dataforseo
    refresh = int(cfg.get("refresh_days", 30))
    plan = await build_plan(db, config, today, save=False)
    seen: set[tuple[int, str]] = set()
    out = []
    for a in plan:
        if a.topic_id is None or a.platform not in set(cfg.get("platforms",
                                                           ["BLOG", "TOOLSITE"])):
            continue
        if (a.topic_id, a.market) in seen:
            continue
        seen.add((a.topic_id, a.market))
        fresh = await db.pool.fetchval(
            "SELECT 1 FROM topic_metrics WHERE topic_id = $1 AND market = $2 "
            "AND source = 'dataforseo' AND metric_name = 'gsv' AND date > $3::date - $4::int LIMIT 1",
            a.topic_id, a.market, today, refresh)
        if not fresh:
            out.append(a)
    return out[: int(cfg.get("max_topics", 20))]


async def run_verify(db: Database, config: Config, secrets: Secrets,
                     client: httpx.AsyncClient) -> dict[str, Any]:
    """Measure today's best blog / tool items with real Google data."""
    dfs = DataForSEO(config, secrets, client, db)
    if not config.dataforseo.get("enabled", True):
        return {"status": "skipped", "reason": "dataforseo.enabled = false"}
    if not dfs.enabled:
        return {"status": "skipped", "reason": "DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD not set"}
    today = datetime.now(UTC).date()
    targets = await _targets(db, config, today)
    if not targets:
        return {"status": "skipped", "reason": "nothing due"}
    longtail_n = int(config.dataforseo.get("longtail_keywords", 9))
    by_market: dict[str, list[tuple[Any, str, list[str]]]] = defaultdict(list)
    for a in targets:
        main = sanitize_keyword(clean_query(a.topic))
        if not main:
            continue
        rows = await db.pool.fetch(
            "SELECT suggestion FROM longtail WHERE topic_id = $1 AND market = $2 "
            "ORDER BY length(suggestion) LIMIT $3", a.topic_id, a.market, longtail_n * 2)
        tail = [k for k in (sanitize_keyword(r["suggestion"]) for r in rows) if k and k != main]
        by_market[a.market].append((a, main, list(dict.fromkeys(tail))[:longtail_n]))
    stats = {"topics": 0, "errors": 0, "budget_stop": False}
    signals: list[RawSignal] = []
    try:
        for code, items in by_market.items():
            market = config.market(code)
            keywords = list(dict.fromkeys(k for _, main, tail in items for k in [main, *tail]))
            vols, _, err = await dfs.volumes(market, keywords)
            if err:
                stats["errors"] += 1
                log.warning("dataforseo.volume_failed", market=code, error=err)
            kds, _, err = await dfs.difficulty(market, [main for _, main, _ in items])
            if err:
                stats["errors"] += 1
                log.warning("dataforseo.kd_failed", market=code, error=err)
            for a, main, tail in items:
                v = vols.get(main) or {}
                metrics: dict[str, float] = {}
                if v.get("volume") is not None:
                    metrics["gsv"] = float(v["volume"])
                if v.get("cpc") is not None:
                    metrics["cpc"] = float(v["cpc"])
                tail_vol = [float((vols.get(k) or {}).get("volume") or 0) for k in tail]
                if tail:
                    metrics["gsv_longtail"] = sum(tail_vol)
                if main in kds:
                    metrics["kd"] = kds[main]
                serp, _, err = await dfs.serp(market, main)
                if err:
                    stats["errors"] += 1
                elif serp:
                    top = serp[:10]
                    metrics["serp_weak_share"] = round(sum(r["weak"] for r in top) / len(top), 3)
                    await db.pool.executemany(
                        "INSERT INTO serp_results (topic_id, market, position, domain, url, "
                        "title, keyword, fetched_at) VALUES ($1,$2,$3,$4,$5,$6,$7,now()) "
                        "ON CONFLICT DO NOTHING",
                        [(a.topic_id, code, r["position"], r["domain"], r["url"], r["title"],
                          main) for r in top])
                for name, value in metrics.items():
                    signals.append(RawSignal("dataforseo", code, main, name, value, today,
                                             topic_key=await _key(db, a.topic_id)))
                for k in tail:
                    vol = (vols.get(k) or {}).get("volume")
                    if vol is not None:
                        await db.pool.execute(
                            "UPDATE longtail SET volume = $4 WHERE topic_id = $1 AND market = $2 "
                            "AND lower(suggestion) = $3", a.topic_id, code, k, int(vol))
                stats["topics"] += 1
    except BudgetExceeded as exc:
        stats["budget_stop"] = True
        log.info("dataforseo.budget_stop", reason=str(exc))
    if signals:
        await db.upsert_signals(signals)
    status = "error" if stats["errors"] and not stats["topics"] else (
        "partial" if stats["errors"] or stats["budget_stop"] else "ok")
    return {"status": status, **stats, "spent_usd": round(dfs.spent, 4)}


async def _key(db: Database, topic_id: int) -> str | None:
    return await db.pool.fetchval("SELECT topic_key FROM topics WHERE id = $1", topic_id)
