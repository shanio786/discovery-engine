"""Shared collector machinery.

Every collector implements the same interface so the pipeline treats them
uniformly and new sources drop in without pipeline changes:

    name / cost            identity + whether it spends money
    is_enabled()           credentials present?            -> skipped if not
    supports(market)       meaningful for this market?     -> skipped if not
    collect(market, since) -> list[RawSignal]              (must be idempotent)

Per-item failures are isolated (counted in `errors`, run marked partial); a
tripped circuit breaker stops scheduling new items but keeps what was fetched.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from typing import Any, Literal, TypeVar

import httpx

from ode.config import Config, Secrets
from ode.http import CircuitOpenError, HttpFetcher
from ode.logging import get_logger
from ode.models import Market, RawSignal
from ode.quota import QuotaTracker
from ode.state import CollectorState, InMemoryState

log = get_logger(__name__)

CostType = Literal["free", "paid"]
T = TypeVar("T")
R = TypeVar("R")


def make_fetcher(config: Config, client: httpx.AsyncClient, source: str) -> HttpFetcher:
    return HttpFetcher(
        client,
        config.rate_limit(source),
        name=source,
        max_retries=config.http_max_retries,
        backoff_base=config.http_backoff_base,
        breaker_threshold=config.circuit_breaker_failures,
    )


class BaseCollector(ABC):
    """Base class. Subclasses set `name`/`cost` and implement `collect()`."""

    name: str = "base"
    cost: CostType = "free"

    def __init__(
        self,
        config: Config,
        secrets: Secrets,
        client: httpx.AsyncClient,
        quota: QuotaTracker,
        state: CollectorState | None = None,
    ) -> None:
        self.config = config
        self.secrets = secrets
        self.client = client
        self.quota = quota
        self.state: CollectorState = state or InMemoryState()
        self.http = make_fetcher(config, client, self.name)
        self.concurrency = config.concurrency(self.name)
        self.log = log.bind(collector=self.name)
        self.reset_run_stats()

    # ---------- per-run bookkeeping ----------
    def reset_run_stats(self) -> None:
        self.last_quota_used = 0
        self.errors = 0
        self.notes: list[str] = []
        self.aborted: str | None = None
        self.skip_reason: str | None = None  # collect() found nothing to do, and why
        self.http.consecutive_failures = 0

    @property
    def partial(self) -> bool:
        return bool(self.errors or self.aborted or self.notes)

    # ---------- capability checks ----------
    def is_enabled(self) -> tuple[bool, str]:
        """(enabled, reason). Disabled collectors are skipped, not failed."""
        return True, ""

    def supports(self, market: Market) -> tuple[bool, str]:
        """(supported, reason). Unsupported markets are skipped, not failed."""
        return True, ""

    @abstractmethod
    async def collect(self, market: Market, since: datetime) -> list[RawSignal]:
        raise NotImplementedError

    # ---------- helpers ----------
    async def charge_quota(self, units: int) -> bool:
        """Reserve units against this source's persisted daily cap. False means
        the cap would be exceeded — stop and mark the run partial."""
        ok = await self.quota.check_and_reserve(self.name, units)
        if ok:
            self.last_quota_used += units
        return ok

    async def _get_json(self, url: str, **kwargs: Any) -> Any | None:
        return await self.http.get_json(url, **kwargs)

    async def map_items(
        self, items: Iterable[T], fn: Callable[[T], Awaitable[R]], *, label: str = "item"
    ) -> list[R]:
        """Run `fn` over items with bounded concurrency and per-item isolation.

        A failing item is logged + counted and skipped. A CircuitOpenError stops
        new items from starting; results already fetched are still returned.
        """
        sem = asyncio.Semaphore(self.concurrency)
        results: list[R] = []

        async def run(item: T) -> None:
            if self.aborted:
                return
            async with sem:
                if self.aborted:
                    return
                try:
                    results.append(await fn(item))
                except CircuitOpenError as exc:
                    if not self.aborted:
                        self.aborted = str(exc)
                        self.log.error("collector.circuit_open", reason=str(exc))
                except Exception as exc:  # noqa: BLE001 - isolate per item
                    self.errors += 1
                    self.log.warning(f"{self.name}.{label}_failed", item=str(item)[:120],
                                     error=str(exc)[:300])

        await asyncio.gather(*(run(i) for i in items))
        return results
