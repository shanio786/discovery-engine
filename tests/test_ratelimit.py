import pytest

from ode.ratelimit import AsyncTokenBucket, RateLimit, RetryableHTTPError, with_backoff


class FakeClock:
    """Deterministic clock: time only advances when the bucket sleeps."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


async def test_bucket_allows_burst_without_waiting():
    clock = FakeClock()
    bucket = AsyncTokenBucket(rate=1.0, burst=3, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        await bucket.acquire()
    assert clock.t == 0.0  # burst consumed instantly


async def test_bucket_throttles_after_burst():
    clock = FakeClock()
    bucket = AsyncTokenBucket(rate=2.0, burst=1, clock=clock, sleep=clock.sleep)
    await bucket.acquire()          # consumes the one token
    await bucket.acquire()          # must wait ~1/rate = 0.5s
    assert clock.t == pytest.approx(0.5, abs=1e-6)


async def test_bucket_rejects_oversized_acquire():
    bucket = AsyncTokenBucket(rate=1.0, burst=2)
    with pytest.raises(ValueError):
        await bucket.acquire(5)


def test_rate_limit_from_limit():
    bucket = AsyncTokenBucket.from_limit(RateLimit(rate=5.0, burst=10))
    assert bucket.rate == 5.0
    assert bucket.capacity == 10


async def test_with_backoff_retries_then_succeeds():
    calls = {"n": 0}

    @with_backoff(max_attempts=3, base_seconds=0.001)
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise RetryableHTTPError("boom")
        return "ok"

    assert await flaky() == "ok"
    assert calls["n"] == 2
