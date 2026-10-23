"""Google Play collector: is there room for YOUR app on this topic?

For each target (seed queries + the best-scoring topics) in each market's
language/country it searches Play, detail-checks the top apps and records:

    ps_results           how many apps the search returns
    ps_top_installs      installs of the biggest top app (= proven demand)
    ps_top_rating        mean rating of the top apps
    ps_low_rated_share   share of top apps rated < 4.0   (users unhappy)
    ps_stale_share       share of top apps not updated for a year (abandoned)
    ps_gap               0-100: unhappy users + abandoned apps = your opening

Uses the unofficial `google-play-scraper` (sync) in a worker thread, throttled
hard. Any failure is isolated: Play changes its markup sometimes, and that
must never break the other sources.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ode.collectors.base import BaseCollector
from ode.http import CircuitOpenError
from ode.models import Market, Probe, RawSignal
from ode.scoring.text import clean_query


def parse_installs(value: Any) -> int | None:
    """1000000 | '1,000,000+' | '1.000.000+' -> 1000000."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return int(value)
    digits = re.sub(r"[^\d]", "", str(value))
    return int(digits) if digits else None


def playstore_metrics(
    results: list[dict[str, Any]],
    details: dict[str, dict[str, Any]],
    *,
    today: date,
    top_n: int = 5,
    stale_after_days: int = 365,
) -> dict[str, float]:
    """Pure: search results + per-app details -> metric dict."""
    top = [r for r in results if r.get("appId")][:top_n]
    out: dict[str, float] = {"ps_results": float(len(results))}
    if not top:
        out["ps_gap"] = 100.0  # nobody serves this on Play at all
        return out
    installs, ratings, low, stale, judged_age = [], [], 0, 0, 0
    for r in top:
        d = details.get(r["appId"], {})
        inst = parse_installs(d.get("realInstalls") or d.get("minInstalls") or r.get("installs"))
        if inst is not None:
            installs.append(inst)
        score = d.get("score", r.get("score"))
        if isinstance(score, int | float) and score > 0:
            ratings.append(float(score))
            low += score < 4.0
        updated = d.get("updated")
        if isinstance(updated, int | float) and updated > 0:
            judged_age += 1
            age = today - datetime.fromtimestamp(updated, tz=UTC).date()
            stale += age > timedelta(days=stale_after_days)
    if installs:
        out["ps_top_installs"] = float(max(installs))
    if ratings:
        out["ps_top_rating"] = round(sum(ratings) / len(ratings), 2)
        out["ps_low_rated_share"] = round(low / len(ratings), 3)
    if judged_age:
        out["ps_stale_share"] = round(stale / judged_age, 3)
    parts = [out[k] for k in ("ps_low_rated_share", "ps_stale_share") if k in out]
    if parts:
        out["ps_gap"] = round(100.0 * sum(parts) / len(parts), 1)
    return out


def _default_search() -> Callable[..., list[dict[str, Any]]]:
    from google_play_scraper import search

    return search


def _default_app() -> Callable[..., dict[str, Any]]:
    from google_play_scraper import app

    return app


class PlayStoreCollector(BaseCollector):
    name = "playstore"
    cost = "free"

    def __init__(self, *args: Any, search_fn: Callable | None = None,
                 app_fn: Callable | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._search = search_fn
        self._app = app_fn

    async def _call(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        """Throttled, isolated call of the sync scraper in a worker thread."""
        if self.http.circuit_open:
            raise CircuitOpenError(f"playstore: {self.http.consecutive_failures} "
                                   "consecutive failures, giving up")
        await self.http.bucket.acquire()
        try:
            result = await asyncio.to_thread(fn, *args, **kwargs)
        except Exception:
            self.http.consecutive_failures += 1
            raise
        self.http.consecutive_failures = 0
        return result

    async def _targets(self, market: Market) -> list[Probe]:
        ps = self.config.playstore
        limit = int(ps.get("max_queries_per_market", 25))
        targets: dict[str, Probe] = {}
        for seed, query in self.config.queries_for(market):
            targets.setdefault(seed.topic_key, Probe(seed.topic_key, query))
        for probe in await self.state.top_topics(market.code, limit):
            targets.setdefault(probe.topic_key, Probe(probe.topic_key, clean_query(probe.query)))
        last = await self.state.last_dates(market.code, self.name, "ps_results")
        refresh = timedelta(days=int(ps.get("refresh_days", 7)))
        today = datetime.now(UTC).date()
        due = [t for t in targets.values()
               if last.get(t.topic_key) is None or today - last[t.topic_key] >= refresh]
        return due[:limit]

    async def collect(self, market: Market, since: datetime) -> list[RawSignal]:
        ps = self.config.playstore
        top_n = int(ps.get("top_apps", 5))
        stale_days = int(ps.get("stale_after_days", 365))
        search = self._search or _default_search()
        app = self._app or _default_app()
        today = datetime.now(UTC).date()
        lang, country = market.language, market.region.lower()
        targets = await self._targets(market)
        if not targets:
            self.skip_reason = f"nothing due on Play Store for {market.code}"
            return []

        async def one(t: Probe) -> list[RawSignal]:
            results = await self._call(search, t.query, n_hits=20, lang=lang, country=country)
            details: dict[str, dict[str, Any]] = {}
            for r in (results or [])[:top_n]:
                if not r.get("appId"):
                    continue
                try:
                    details[r["appId"]] = await self._call(app, r["appId"], lang=lang,
                                                           country=country)
                except CircuitOpenError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one app must not sink the topic
                    self.log.debug("playstore.app_failed", app=r["appId"], error=str(exc)[:200])
            metrics = playstore_metrics(results or [], details, today=today, top_n=top_n,
                                        stale_after_days=stale_days)
            return [RawSignal("playstore", market.code, t.query, k, v, today,
                              raw={"query": t.query}, topic_key=t.topic_key)
                    for k, v in metrics.items()]

        batches = await self.map_items(targets, one, label="query")
        return [s for b in batches for s in b]
