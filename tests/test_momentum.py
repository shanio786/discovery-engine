"""Momentum math, including the spec's required edge cases:
sparse series, single-day spikes, all-zeros, missing days."""

from datetime import date, timedelta

import numpy as np
import pytest

from ode.scoring.momentum import MomentumParams, dense_window, momentum_profile, trailing_median

AS_OF = date(2026, 6, 30)
P = MomentumParams()


def series(values, end=AS_OF):
    n = len(values)
    return {end - timedelta(days=n - 1 - i): float(v) for i, v in enumerate(values)}


def weekly(values, amp=0.15):
    """Add weekly seasonality (weekends lower) so smoothing is exercised."""
    return [v * (1 - amp if i % 7 in (5, 6) else 1) for i, v in enumerate(values)]


t = np.arange(180, dtype=float)


def test_sparse_series_is_unknown_with_neutral_multiplier():
    m = momentum_profile(series([100] * 40), AS_OF, P)
    assert m.phase == "UNKNOWN" and m.multiplier == 1.0 and "40 days" in m.reason


def test_all_zeros_is_unknown():
    m = momentum_profile(series([0] * 180), AS_OF, P)
    assert m.phase == "UNKNOWN" and m.multiplier == 1.0 and m.reason == "no real demand"


def test_empty_series():
    m = momentum_profile({}, AS_OF, P)
    assert m.phase == "UNKNOWN" and m.points == 0


def test_flat_is_stable():
    rng = np.random.default_rng(1)
    m = momentum_profile(series(weekly(500 + rng.normal(0, 10, 180))), AS_OF, P)
    assert m.phase == "STABLE" and m.multiplier == 1.0


def test_linear_growth_is_growing():
    m = momentum_profile(series(weekly(100 + 0.6 * t)), AS_OF, P)
    assert m.phase == "GROWING" and m.velocity > P.velocity_threshold


def test_accelerating_growth_is_breakout():
    m = momentum_profile(series(weekly(100 + 0.01 * t**2)), AS_OF, P)
    assert m.phase == "BREAKOUT" and m.multiplier == 3.0 and m.acceleration > P.breakout_accel


def test_flattening_growth_is_peaking():
    m = momentum_profile(series(weekly(100 + 60 * np.sqrt(t))), AS_OF, P)
    assert m.phase == "PEAKING" and m.multiplier == 0.6


def test_decline_is_declining():
    m = momentum_profile(series(weekly(800 - 3 * t)), AS_OF, P)
    assert m.phase == "DECLINING" and m.multiplier == 0.3


def test_single_day_news_spike_is_not_a_breakout():
    flat = [500.0] * 180
    flat[-1] = 50_000  # one viral news day, today
    assert momentum_profile(series(flat), AS_OF, P).phase == "STABLE"
    mid = [500.0] * 180
    mid[120] = 50_000  # and one in the middle of the window
    assert momentum_profile(series(mid), AS_OF, P).phase == "STABLE"


def test_four_day_spike_is_still_ignored_but_a_sustained_rise_counts():
    short = [500.0] * 176 + [5_000.0] * 4
    assert momentum_profile(series(short), AS_OF, P).phase == "STABLE"
    sustained = [500.0] * 150 + [500.0 * 1.05 ** i for i in range(30)]
    assert momentum_profile(series(sustained), AS_OF, P).phase == "BREAKOUT"


def test_flat_then_rising_does_not_explode():
    """First-half slope ~0 used to divide by ~0; now finite and still BREAKOUT."""
    y = [300.0] * 90 + list(300 + 4.0 * np.arange(90))
    m = momentum_profile(series(y), AS_OF, P)
    assert m.phase == "BREAKOUT"
    assert np.isfinite(m.acceleration) and m.acceleration < 1e6


def test_missing_days_are_interpolated_not_zeroed():
    full = series(weekly(100 + 0.01 * t**2))
    rng = np.random.default_rng(7)
    holey = {d: v for d, v in full.items() if rng.random() > 0.2}  # drop ~20% of days
    m = momentum_profile(holey, AS_OF, P)
    assert m.phase == "BREAKOUT" and m.points == len(holey)


def test_no_lookahead_future_rows_are_ignored():
    past = series(weekly(800 - 3 * t))
    future = {AS_OF + timedelta(days=i): 1e6 for i in range(1, 30)}
    assert momentum_profile({**past, **future}, AS_OF, P) == momentum_profile(past, AS_OF, P)


def test_window_limits_history():
    y, real = dense_window(series([1.0] * 400), AS_OF, 180)
    assert len(y) == 180 and real == 180


def test_trailing_median_is_causal():
    y = np.array([1, 1, 1, 1, 1, 9, 9, 9, 9, 9], dtype=float)
    out = trailing_median(y, 9)
    assert list(out[:5]) == [1, 1, 1, 1, 1]           # nothing leaks from the future
    assert out[-1] == 9                               # 5 of last 9 elevated -> counts


def test_change30_and_spike_ratio():
    y = [100.0] * 150 + [200.0] * 30
    m = momentum_profile(series(y), AS_OF, P)
    assert m.change30 == pytest.approx(1.0)
    assert m.mean30 == pytest.approx(200.0)


def test_thresholds_come_from_config():
    params = MomentumParams.from_config({"min_series_points": 10, "momentum": {
        "velocity_threshold": 0.5, "multipliers": {"GROWING": 2.5}}})
    assert params.min_points == 10 and params.velocity_threshold == 0.5
    assert params.multipliers["GROWING"] == 2.5 and params.multipliers["BREAKOUT"] == 3.0
    # with a huge velocity threshold, linear growth becomes STABLE
    assert momentum_profile(series(100 + 0.6 * t), AS_OF, params).phase == "STABLE"


def _phase_rates(shape, noise, n=150, seed=0):
    rng = np.random.default_rng(seed)
    counts: dict[str, int] = {}
    for _ in range(n):
        y = np.maximum(0, shape * rng.normal(1, noise, 180))
        ph = momentum_profile(series(y), AS_OF, P).phase
        counts[ph] = counts.get(ph, 0) + 1
    return {k: v / n for k, v in counts.items()}


def test_noise_robustness_regression():
    """Statistical guard: real-world noise must not flip phases (the reason the
    curvature has to pass a significance test)."""
    linear = _phase_rates(500 * (1 + 0.004 * t), 0.10)
    assert linear.get("GROWING", 0) >= 0.85 and linear.get("BREAKOUT", 0) <= 0.08
    quad = _phase_rates(500 * (1 + 0.00005 * t**2), 0.10)
    assert quad.get("BREAKOUT", 0) >= 0.95
    flat = _phase_rates(500 + 0 * t, 0.15)
    assert flat.get("STABLE", 0) >= 0.95 and "BREAKOUT" not in flat
    decline = _phase_rates(500 * (1 - 0.003 * t), 0.10)
    assert decline.get("DECLINING", 0) >= 0.95


def test_insignificant_curvature_is_reported_as_such():
    rng = np.random.default_rng(3)
    m = momentum_profile(series(500 * (1 + 0.004 * t) * rng.normal(1, 0.1, 180)), AS_OF, P)
    if m.accel_t is not None and abs(m.accel_t) < P.accel_min_t:
        assert "not significant" in m.reason and m.phase == "GROWING"
