"""Hacker News collector via the Algolia search API.

Keyless, unlimited. Signal = volume of story mentions per day for a seed term
(early tech demand, often 3-6 months ahead of the mainstream). HN is English,
so we only run it for English markets.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Any

from ode.collectors.base import BaseCollector
from ode.models import Market, RawSignal

SEARCH = "https://hn.algolia.com/api/v1/search_by_date"


def parse_hn(payload: Any, market: str, query: str) -> list[RawSignal]:
    """Pure parser: aggregate Algolia hits into per-day mention counts."""
    if not payload:
        return []
    by_day: dict[date, int] = defaultdict(int)
    for hit in payload.get("hits", []):
        ts = hit.get("created_at_i")
        if ts is None:
            continue
        try:
            d = datetime.fromtimestamp(int(ts), tz=UTC).date()
        except (ValueError, OSError, OverflowError):
            continue
        by_day[d] += 1
    return [
        RawSignal(
            source="hackernews",
            market=market,
            topic_surface_form=query,
            metric_name="hn_mentions",
            value=float(count),
            observed_at=d,
            raw={"query": query},
        )
        for d, count in sorted(by_day.items())
    ]


class HackerNewsCollector(BaseCollector):
    name = "hackernews"
    cost = "free"

    async def collect(self, market: Market, since: datetime) -> list[RawSignal]:
        if market.language != "en":
            return []  # HN is English-only; avoid polluting other markets
        since_ts = int(since.replace(tzinfo=UTC).timestamp())
        signals: list[RawSignal] = []
        for query in self.config.seed_queries:
            payload = await self._get_json(
                SEARCH,
                params={
                    "query": query,
                    "tags": "story",
                    "numericFilters": f"created_at_i>{since_ts}",
                    "hitsPerPage": 1000,
                },
            )
            parsed = parse_hn(payload, market.code, query)
            signals.extend(parsed)
            self.log.debug("hn.query_ok", query=query, days=len(parsed))
        return signals
