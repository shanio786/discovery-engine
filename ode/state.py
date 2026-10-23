"""What collectors may ask about the past: their watchlist and how far each
topic's history already reaches (for incremental fetching).

The pipeline passes a DB-backed implementation; tests pass InMemoryState.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Protocol

from ode.models import Probe, WatchItem


class CollectorState(Protocol):
    async def watchlist(self, market: str, kind: str) -> list[WatchItem]: ...

    async def last_dates(self, market: str, source: str, metric: str) -> dict[str, date]:
        """Latest stored observation date per topic_key."""
        ...

    async def observed_dates(self, market: str, source: str, metric: str
                             ) -> dict[str, set[date]]:
        """Every stored observation date per topic_key (for one-off backfills)."""
        ...

    async def pending_probes(self, market: str, limit: int) -> list[Probe]: ...

    async def probed(self, market: str) -> list[Probe]: ...

    async def mark_probed(self, market: str, topic_key: str) -> None: ...

    async def top_topics(self, market: str, limit: int) -> list[Probe]:
        """Best-scoring topics in the latest scores (topic_key, display name)."""
        ...


class InMemoryState:
    """Simple dict-backed state for tests and dry runs."""

    def __init__(
        self,
        watch: dict[tuple[str, str], list[WatchItem]] | None = None,
        last: dict[tuple[str, str, str], dict[str, date]] | None = None,
        observed: dict[tuple[str, str, str], dict[str, set[date]]] | None = None,
        probes: dict[str, list[Probe]] | None = None,
        top: dict[str, list[Probe]] | None = None,
    ) -> None:
        self._watch = watch or {}
        self._last = last or {}
        self._observed = observed or {}
        self._pending = {m: list(v) for m, v in (probes or {}).items()}
        self._done: dict[str, list[Probe]] = {}
        self._top = top or {}

    async def watchlist(self, market: str, kind: str) -> list[WatchItem]:
        return list(self._watch.get((market, kind), []))

    async def last_dates(self, market: str, source: str, metric: str) -> dict[str, date]:
        return dict(self._last.get((market, source, metric), {}))

    async def observed_dates(self, market: str, source: str, metric: str
                             ) -> dict[str, set[date]]:
        return {k: set(v) for k, v in self._observed.get((market, source, metric), {}).items()}

    async def pending_probes(self, market: str, limit: int) -> list[Probe]:
        return self._pending.get(market, [])[:limit]

    async def probed(self, market: str) -> list[Probe]:
        return list(self._done.get(market, []))

    async def top_topics(self, market: str, limit: int) -> list[Probe]:
        return self._top.get(market, [])[:limit]

    async def mark_probed(self, market: str, topic_key: str) -> None:
        pending = self._pending.get(market, [])
        for p in [p for p in pending if p.topic_key == topic_key]:
            pending.remove(p)
            self._done.setdefault(market, []).append(p)


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
