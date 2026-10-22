"""Per-API daily quota tracking.

Collectors call `check_and_reserve()` BEFORE spending units; it refuses
(returns False) rather than letting a run blow past the daily cap. The store
is pluggable: in-memory for tests, Postgres for production.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Protocol


def _today() -> date:
    return datetime.now(UTC).date()


class QuotaStore(Protocol):
    async def get_used(self, api: str, day: date) -> int: ...
    async def add_used(self, api: str, day: date, units: int, daily_limit: int) -> None: ...


class InMemoryQuotaStore:
    """Non-persistent store for tests and dry runs."""

    def __init__(self) -> None:
        self._used: dict[tuple[str, date], int] = {}

    async def get_used(self, api: str, day: date) -> int:
        return self._used.get((api, day), 0)

    async def add_used(self, api: str, day: date, units: int, daily_limit: int) -> None:
        self._used[(api, day)] = self._used.get((api, day), 0) + units


class QuotaTracker:
    """Enforces daily caps. `limits[api] is None` => untracked/unlimited."""

    def __init__(self, store: QuotaStore, limits: dict[str, int | None]) -> None:
        self.store = store
        self.limits = limits

    async def remaining(self, api: str, day: date | None = None) -> int | None:
        limit = self.limits.get(api)
        if limit is None:
            return None
        used = await self.store.get_used(api, day or _today())
        return max(0, limit - used)

    async def check_and_reserve(
        self, api: str, units: int, day: date | None = None
    ) -> bool:
        """Reserve `units` if they fit under today's cap. Returns success."""
        day = day or _today()
        limit = self.limits.get(api)
        if limit is None:
            return True  # untracked source — always allowed
        used = await self.store.get_used(api, day)
        if used + units > limit:
            return False
        await self.store.add_used(api, day, units, limit)
        return True
