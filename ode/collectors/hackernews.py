"""Hacker News collector via the Algolia search API.

Keyless, unlimited. Signal = exact number of stories per day mentioning a
seed's core term (early tech demand, often months ahead of the mainstream).

Counts come from `nbHits` of one query PER DAY (hitsPerPage=0), so they are
never truncated by Algolia's 1000-result cap, and zero days are real zeros.
HN is one global English audience, so it is attributed to a single market
(`hackernews.market`, default: the first English market) — never duplicated.
"""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime
from typing import Any

from ode.collectors.base import BaseCollector
from ode.http import UpstreamError
from ode.models import Market, RawSignal, Seed
from ode.state import day_range, incremental_start

SEARCH = "https://hn.algolia.com/api/v1/search_by_date"


def parse_nbhits(payload: Any) -> int | None:
    """Algolia payload -> total hit count, or None if the payload is unusable."""
    if not isinstance(payload, dict):
        return None
    n = payload.get("nbHits")
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        return None
    return n


def day_bounds(d: date) -> tuple[int, int]:
    """[start, end) unix seconds for a UTC calendar day."""
    start = calendar.timegm(d.timetuple())
    return start, start + 86_400


class HackerNewsCollector(BaseCollector):
    name = "hackernews"
    cost = "free"

    def _market_code(self) -> str | None:
        configured = (self.config.raw().get("hackernews") or {}).get("market")
        if configured:
            return configured
        return next((m.code for m in self.config.markets() if m.language == "en"), None)

    def supports(self, market: Market) -> tuple[bool, str]:
        target = self._market_code()
        if market.code != target:
            return False, f"Hacker News is one global English audience, attributed to {target}"
        return True, ""

    async def collect(self, market: Market, since: datetime) -> list[RawSignal]:
        last = await self.state.last_dates(market.code, self.name, "hn_mentions")
        today = datetime.now(UTC).date()
        lookback_start = since.date()
        overlap = self.config.overlap_days(self.name)

        tasks: list[tuple[Seed, date]] = []
        for seed in self.config.seeds:
            start = incremental_start(
                last.get(seed.topic_key), lookback_start=lookback_start, overlap_days=overlap
            )
            tasks.extend((seed, d) for d in day_range(start, today))

        async def one(task: tuple[Seed, date]) -> RawSignal:
            seed, d = task
            t0, t1 = day_bounds(d)
            payload = await self.http.get_json(
                SEARCH,
                params={
                    "query": seed.hn_term,
                    "tags": "story",
                    "hitsPerPage": 0,
                    "numericFilters": f"created_at_i>={t0},created_at_i<{t1}",
                },
            )
            n = parse_nbhits(payload)
            if n is None:  # never write a fake zero
                raise UpstreamError(f"unusable HN payload for {seed.key} {d}")
            return RawSignal("hackernews", market.code, seed.hn_term, "hn_mentions", float(n),
                             d, raw={"term": seed.hn_term}, topic_key=seed.topic_key)

        signals = await self.map_items(tasks, one, label="day")
        self.log.info("hn.collected", queries=len(self.config.seeds), days=len(tasks),
                      signals=len(signals), errors=self.errors)
        return signals



