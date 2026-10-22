from datetime import date

from ode.state import day_range, incremental_start

LB = date(2024, 1, 1)


def test_incremental_start_new_topic_uses_full_lookback():
    assert incremental_start(None, lookback_start=LB, overlap_days=3) == LB


def test_incremental_start_known_topic_refetches_overlap():
    assert incremental_start(date(2024, 3, 10), lookback_start=LB, overlap_days=3) == date(2024, 3, 7)


def test_incremental_start_never_before_lookback():
    assert incremental_start(date(2023, 6, 1), lookback_start=LB, overlap_days=3) == LB


def test_day_range():
    assert day_range(date(2024, 1, 30), date(2024, 2, 1)) == [
        date(2024, 1, 30), date(2024, 1, 31), date(2024, 2, 1)]
    assert day_range(date(2024, 2, 2), date(2024, 2, 1)) == []
