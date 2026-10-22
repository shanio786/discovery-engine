"""Shared collector machinery: HTTP+rate-limit+backoff+quota.

Every collector implements the same interface so the pipeline treats them
uniformly and new sources drop in without pipeline changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Literal

import httpx

from ode.config import Config, Secrets
from ode.logging import get_logger
from ode.models import Market, RawSignal
from ode.quota import QuotaTracker
from ode.ratelimit import AsyncTokenBucket, RetryableHTTPError, with_backoff

log = get_logger(__name__)

CostType = Literal["free", "paid"]


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
    ) -> None:
        self.config = config
        self.secrets = secrets
        self.client = client
        self.quota = quota
        rl = config.rate_limit(self.name)
        self.bucket = AsyncTokenBucket.from_limit(rl)
        self.log = log.bind(collector=self.name)
        # units charged against the persisted daily quota in the last collect()
        self.last_quota_used = 0

    async def charge_quota(self, units: int) -> bool:
        """Reserve `units` against this source's persisted daily cap.

        Returns False if the cap would be exceeded (caller should stop and mark
        the run partial rather than overspend).
        """
        ok = await self.quota.check_and_reserve(self.name, units)
        if ok:
            self.last_quota_used += units
        return ok

    # -- override to declare readiness (e.g. API key present) --
    def is_enabled(self) -> tuple[bool, str]:
        """Return (enabled, reason). Disabled collectors are skipped, not failed."""
        return True, ""

    @abstractmethod
    async def collect(self, market: Market, since: datetime) -> list[RawSignal]:
        """Harvest signals for one market since `since`. Must be idempotent."""
        raise NotImplementedError

    # -- shared HTTP helper --
    async def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        cost_units: int = 1,
    ) -> Any | None:
        """Rate-limited GET returning parsed JSON, or None on 404.

        Retries transient failures with exponential backoff. `cost_units` is
        charged (locally) to the token bucket; API quota is handled by callers
        that need persisted accounting.
        """

        @with_backoff(
            max_attempts=self.config.http_max_retries,
            base_seconds=self.config.http_backoff_base,
        )
        async def _do() -> Any | None:
            await self.bucket.acquire(cost_units)
            resp = await self.client.get(url, params=params, headers=headers)
            if resp.status_code == 404:
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                raise RetryableHTTPError(f"{resp.status_code} for {url}")
            resp.raise_for_status()
            return resp.json()

        return await _do()
