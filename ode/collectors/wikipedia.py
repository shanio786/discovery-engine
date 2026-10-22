"""Wikipedia Pageviews collector — the demand backbone.

Free, unlimited, multi-year daily history in every language. It tracks the
market's watchlist (seeds + discovered topics + cross-language expansions,
maintained by `ode discover`), fetches only days it doesn't have yet (plus a
small overlap for late upstream data) and zero-fills interior gaps so the
Phase-2 momentum math gets a clean daily series.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

from ode.collectors.base import BaseCollector
from ode.models import Market, RawSignal, WatchItem
from ode.state import incremental_start
from ode.wikimeta import norm_title

REST_BASE = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
WATCH_KIND = "wiki_article"


def _parse_ts(ts: str) -> date:
    # timestamps look like "2024010100" (YYYYMMDDHH)
    return datetime.strptime(ts[:8], "%Y%m%d").date()


def parse_pageviews(
    payload: Any, market: str, article: str, topic_key: str | None = None
) -> list[RawSignal]:
    """Pure parser: Wikimedia JSON -> daily pageview signals (date-sorted)."""
    if not isinstance(payload, dict):
        return []
    by_day: dict[date, float] = {}
    for item in payload.get("items") or []:
        try:
            by_day[_parse_ts(item["timestamp"])] = float(item["views"])
        except (KeyError, ValueError, TypeError):
            continue
    surface = norm_title(article)
    return [
        RawSignal(
            source="wikipedia",
            market=market,
            topic_surface_form=surface,
            metric_name="pageviews",
            value=views,
            observed_at=d,
            raw={"article": article},
            topic_key=topic_key,
        )
        for d, views in sorted(by_day.items())
    ]


def fill_gaps(signals: list[RawSignal]) -> list[RawSignal]:
    """Zero-fill missing days strictly BETWEEN the first and last observation.

    Wikimedia omits zero-view days. Interior gaps are therefore true zeros;
    trailing days are not filled (they may simply not be published yet).
    """
    if len(signals) < 2:
        return signals
    have = {s.observed_at: s for s in signals}
    first, last = signals[0], signals[-1]
    out: list[RawSignal] = []
    d = first.observed_at
    while d <= last.observed_at:
        out.append(
            have.get(d)
            or RawSignal(first.source, first.market, first.topic_surface_form, first.metric_name,
                         0.0, d, raw={"filled": True}, topic_key=first.topic_key)
        )
        d += timedelta(days=1)
    return out


class WikipediaCollector(BaseCollector):
    name = "wikipedia"
    cost = "free"

    async def _items(self, market: Market) -> list[WatchItem]:
        items = await self.state.watchlist(market.code, WATCH_KIND)
        if not items and market.language == "en":
            # Discovery hasn't run yet: fall back to the English seed titles.
            self.log.warning("wiki.empty_watchlist", market=market.code,
                             hint="run `ode discover` (or `ode daily`)")
            items = [WatchItem(s.wiki, s.topic_key) for s in self.config.seeds if s.wiki]
        seen: set[str] = set()
        unique: list[WatchItem] = []
        for it in items:  # one title per topic per market
            if it.topic_key not in seen:
                seen.add(it.topic_key)
                unique.append(it)
        return unique

    async def collect(self, market: Market, since: datetime) -> list[RawSignal]:
        items = await self._items(market)
        if not items:
            self.skip_reason = f"watchlist empty for {market.code} — run `ode discover`"
            return []
        last = await self.state.last_dates(market.code, self.name, "pageviews")
        end = datetime.now(UTC).date() - timedelta(days=1)  # today is never published
        lookback_start = since.date()
        overlap = self.config.overlap_days(self.name)

        async def one(item: WatchItem) -> list[RawSignal]:
            start = incremental_start(
                last.get(item.topic_key), lookback_start=lookback_start, overlap_days=overlap
            )
            if start > end:
                return []
            url = (
                f"{REST_BASE}/{market.wiki_project}/all-access/user/"
                f"{quote(item.term.replace(' ', '_'), safe='')}/daily/"
                f"{start:%Y%m%d}/{end:%Y%m%d}"
            )
            payload = await self.http.get_json(url)
            if payload is None:  # no data in range / page gone — expected
                return []
            return fill_gaps(parse_pageviews(payload, market.code, item.term, item.topic_key))

        batches = await self.map_items(items, one, label="article")
        signals = [s for batch in batches for s in batch]
        self.log.info("wiki.collected", market=market.code, articles=len(items),
                      signals=len(signals), errors=self.errors)
        return signals
