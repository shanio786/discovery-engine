from datetime import date

from ode.quota import InMemoryQuotaStore, QuotaTracker

DAY = date(2024, 1, 1)


async def test_untracked_source_always_allowed():
    t = QuotaTracker(InMemoryQuotaStore(), {"reddit": None})
    for _ in range(100):
        assert await t.check_and_reserve("reddit", 10, DAY) is True
    assert await t.remaining("reddit", DAY) is None


async def test_reserve_up_to_cap_then_refuse():
    t = QuotaTracker(InMemoryQuotaStore(), {"youtube": 100})
    assert await t.check_and_reserve("youtube", 60, DAY) is True
    assert await t.remaining("youtube", DAY) == 40
    assert await t.check_and_reserve("youtube", 60, DAY) is False  # would exceed
    assert await t.remaining("youtube", DAY) == 40                 # unchanged
    assert await t.check_and_reserve("youtube", 40, DAY) is True   # exact fit
    assert await t.remaining("youtube", DAY) == 0


async def test_quota_is_per_day():
    store = InMemoryQuotaStore()
    t = QuotaTracker(store, {"youtube": 100})
    assert await t.check_and_reserve("youtube", 100, date(2024, 1, 1)) is True
    assert await t.check_and_reserve("youtube", 100, date(2024, 1, 2)) is True  # fresh day
