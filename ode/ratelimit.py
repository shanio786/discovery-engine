"""Async token-bucket rate limiter + retry/backoff helper.

The bucket uses an injectable clock + sleep so the math is unit-testable
without real time.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


@dataclass(frozen=True)
class RateLimit:
    rate: float       # tokens (requests) per second
    burst: int = 1    # bucket capacity


class AsyncTokenBucket:
    """Classic token bucket. `acquire()` blocks until a token is available."""

    def __init__(
        self,
        rate: float,
        burst: int = 1,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate = rate
        self.capacity = max(1, burst)
        self.tokens = float(self.capacity)
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._updated
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self._updated = now

    async def acquire(self, n: int = 1) -> None:
        if n > self.capacity:
            raise ValueError(f"cannot acquire {n} > capacity {self.capacity}")
        async with self._lock:
            while True:
                self._refill()
                if self.tokens >= n:
                    self.tokens -= n
                    return
                wait = (n - self.tokens) / self.rate
                await self._sleep(wait)

    @classmethod
    def from_limit(cls, limit: RateLimit, **kwargs) -> AsyncTokenBucket:
        return cls(limit.rate, limit.burst, **kwargs)


class RetryableHTTPError(Exception):
    """Raised by collectors for transient HTTP failures worth retrying."""


def with_backoff(max_attempts: int = 4, base_seconds: float = 2.0):
    """Decorator: retry on RetryableHTTPError with exponential backoff.

    base 2.0 -> waits ~2, 4, 8, 16s (spec's retry policy).
    """
    return retry(
        retry=retry_if_exception_type(RetryableHTTPError),
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=base_seconds, min=base_seconds, max=60),
        reraise=True,
    )
