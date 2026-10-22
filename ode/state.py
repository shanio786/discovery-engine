"""What collectors may ask about the past: their watchlist and how far each
topic's history already reaches (for incremental fetching).

The pipeline passes a DB-backed implementation; tests pass InMemoryState.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Protocol

from ode.models import WatchItem


class CollectorState(Protocol):
    async def watchlist(self, market: str, kind: str) -> list[WatchItem]: ...

    async def last_dates(self, market: str, source: str, metric: str) -> dict[str, date]:
        """Latest stored observation date per topic_key."""
        ...


class InMemoryState:
    """Simple dict-backed state for tests and dry runs."""

    def __init__(
        self,
        watch: dict[tuple[str, str], list[WatchItem]] | None = None,
        last: dict[tuple[str, str, str], dict[str, date]] | None = None,
    ) -> None:
        self._watch = watch or {}
        self._last = last or {}

    async def watchlist(self, market: str, kind: str) -> list[WatchItem]:
        return list(self._watch.get((market, kind), []))

    async def last_dates(self, market: str, source: str, metric: str) -> dict[str, date]:
        return dict(self._last.get((market, source, metric), {}))


def incremental_start(last: date | None, *, lookback_start: date, overlap_days: int) -> date:
    """First day to fetch for one topic.

    Never-seen topic -> full lookback. Known topic -> re-fetch the last
    `overlap_days` (upstream revises recent days), but never earlier than the
    lookback window.
    """
    if last is None:
        return lookback_start
    return max(lookback_start, last - timedelta(days=max(0, overlap_days)))


def day_range(start: date, end: date) -> list[date]:
    """Inclusive list of days from start to end (empty if start > end)."""
    if start > end:
        return []
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]
