"""Momentum: is the window still OPENING?

Implements the spec's second-derivative model:

    velocity     = slope / mean                        (relative growth per day)
    acceleration = (slope_2nd_half - slope_1st_half) / |slope_1st_half|

with three fixes the spec asks for or needs:

  * Sparse series (< min_points real observations) or no real demand
    -> UNKNOWN, multiplier 1.0.
  * Short news spikes don't count: the series first goes through a TRAILING
    rolling median of 2*persist-1 days, so a rise must hold for `persist`
    days before it moves the curve (trailing = no lookahead, backtest-safe).
  * Both half-slopes come from ONE quadratic fit on weekly medians, and the
    curvature only counts when it is statistically significant (|t| >=
    accel_min_t). Real Wikipedia noise otherwise flips steady growth into
    BREAKOUT/PEAKING at the spec's tight +0.3/-0.1 thresholds.
  * The raw acceleration ratio explodes when the first-half slope is ~0
    (flat-then-rising topics — exactly the ones we want). The denominator is
    floored at `velocity_threshold * mean`, so the ratio keeps the spec's
    meaning and thresholds but stays finite.

Missing days are linearly interpolated (Wikipedia zero-days are already
stored as real zeros by the collector, so remaining gaps are fetch gaps).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

DEFAULT_MULTIPLIERS = {"BREAKOUT": 3.0, "GROWING": 1.8, "PEAKING": 0.6, "STABLE": 1.0,
                       "DECLINING": 0.3, "UNKNOWN": 1.0}


@dataclass(frozen=True)
class MomentumParams:
    min_points: int = 60
    min_mean: float = 1.0
    persist_days: int = 5
    window_days: int = 180
    velocity_threshold: float = 0.002
    breakout_accel: float = 0.3
    growing_accel: float = -0.1
    accel_min_t: float = 2.0     # curvature must be this many standard errors from 0
    multipliers: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_MULTIPLIERS))

    @classmethod
    def from_config(cls, scoring: Mapping) -> MomentumParams:
        m = scoring.get("momentum") or {}
        return cls(
            min_points=int(scoring.get("min_series_points", 60)),
            min_mean=float(scoring.get("min_mean_daily", 1.0)),
            persist_days=int(scoring.get("spike_persist_days", 5)),
            window_days=int(scoring.get("momentum_window_days", 180)),
            velocity_threshold=float(m.get("velocity_threshold", 0.002)),
            breakout_accel=float(m.get("breakout_accel", 0.3)),
            growing_accel=float(m.get("growing_accel", -0.1)),
            accel_min_t=float(m.get("accel_min_t", 2.0)),
            multipliers={**DEFAULT_MULTIPLIERS, **(m.get("multipliers") or {})},
        )


@dataclass(frozen=True)
class Momentum:
    phase: str
    multiplier: float
    velocity: float | None = None
    acceleration: float | None = None
    points: int = 0
    accel_t: float | None = None       # t-statistic of the curvature (|t| < min -> noise)
    spike_ratio: float | None = None   # last 7 days vs the 7 before (raw)
    mean30: float | None = None        # mean of the last 30 days (raw)
    change30: float | None = None      # last 30 days vs the 30 before, as a ratio - 1
    reason: str = ""


def dense_window(series: Mapping[date, float], as_of: date, window_days: int) -> tuple[np.ndarray, int]:
    """Daily array over [as_of - window + 1, last observation <= as_of].

    Returns (values with gaps linearly interpolated, number of real points).
    Observations after `as_of` are ignored (no lookahead).
    """
    obs = {d: float(v) for d, v in series.items() if d <= as_of and v is not None}
    if not obs:
        return np.array([]), 0
    end = max(obs)
    start = max(min(obs), as_of - timedelta(days=window_days - 1))
    if start > end:
        return np.array([]), 0
    n = (end - start).days + 1
    y = np.full(n, np.nan)
    for d, v in obs.items():
        if start <= d <= end:
            y[(d - start).days] = v
    real = int(np.count_nonzero(~np.isnan(y)))
    if real == 0:
        return np.array([]), 0
    idx = np.arange(n)
    mask = ~np.isnan(y)
    y = np.interp(idx, idx[mask], y[mask])
    return y, real


def trailing_median(y: np.ndarray, window: int) -> np.ndarray:
    """Causal rolling median (uses only today and the past)."""
    if window <= 1 or len(y) == 0:
        return y.copy()
    padded = np.concatenate([np.full(window - 1, np.nan), y])
    return np.nanmedian(np.lib.stride_tricks.sliding_window_view(padded, window), axis=1)


def _slope(y: np.ndarray) -> float:
    if len(y) < 2:
        return 0.0
    x = np.arange(len(y), dtype=float)
    return float(np.polyfit(x, y, 1)[0])


def weekly_medians(y: np.ndarray) -> np.ndarray:
    """Medians of complete 7-day blocks counted back from the latest day.

    Medians, not means: a news spike of up to 3 days can't move a week, and
    blocks don't overlap, so weeks stay near-independent for the t-test."""
    k = len(y) // 7
    return np.median(y[len(y) - 7 * k:].reshape(k, 7), axis=1) if k else np.array([])


def half_slopes(w: np.ndarray) -> tuple[float, float, float]:
    """(slope at 1/4, slope at 3/4, curvature t-statistic) from ONE quadratic fit.

    Fitted on weekly medians (near-independent, weekly seasonality gone), so the
    standard error is honest. A curvature within noise (|t| small) must not
    turn steady growth into BREAKOUT or PEAKING.
    """
    n = len(w)
    if n < 6:
        return 0.0, 0.0, 0.0
    x = np.arange(n, dtype=float)
    (c, b, _), cov = np.polyfit(x, w, 2, cov=True)
    se = float(np.sqrt(max(cov[0, 0], 0.0)))
    t = float(c / se) if se > 0 else (0.0 if c == 0 else float("inf") * np.sign(c))
    return float(b + 2 * c * (n / 4)), float(b + 2 * c * (3 * n / 4)), t


def momentum_profile(series: Mapping[date, float], as_of: date,
                     params: MomentumParams | None = None) -> Momentum:
    p = params or MomentumParams()
    mult = p.multipliers
    y, real = dense_window(series, as_of, p.window_days)

    def ratio(a: np.ndarray, b: np.ndarray) -> float | None:
        return float(a.sum() / b.sum() - 1.0) if len(a) and len(b) and b.sum() > 0 else None

    mean30 = float(y[-30:].mean()) if len(y) else None
    spike = (float(y[-7:].sum() / (y[-14:-7].sum() + 1.0)) if len(y) >= 14 else None)
    change30 = ratio(y[-30:], y[-60:-30]) if len(y) >= 60 else None
    base = {"points": real, "spike_ratio": spike, "mean30": mean30, "change30": change30}

    if real < p.min_points:
        return Momentum("UNKNOWN", mult["UNKNOWN"], reason=f"only {real} days of data", **base)
    if float(y.mean()) < p.min_mean:
        return Momentum("UNKNOWN", mult["UNKNOWN"], reason="no real demand", **base)

    filtered = trailing_median(y, 2 * p.persist_days - 1)
    smooth = np.convolve(filtered, np.ones(7) / 7, mode="valid")  # kill weekly seasonality
    mean_s = float(smooth.mean())
    velocity = _slope(smooth) / (mean_s + 1e-9)
    weeks = weekly_medians(y)  # raw days: independent weeks for an honest t-test
    s1, s2, accel_t = half_slopes(weeks)
    floor = max(p.velocity_threshold * 7 * float(weeks.mean()), 1e-9)  # per-week units
    acceleration = (s2 - s1) / max(abs(s1), floor)
    accel_for_phase = acceleration if abs(accel_t) >= p.accel_min_t else 0.0

    v = p.velocity_threshold
    if velocity > v and accel_for_phase > p.breakout_accel:
        phase = "BREAKOUT"
    elif velocity > v and accel_for_phase > p.growing_accel:
        phase = "GROWING"
    elif velocity > v:
        phase = "PEAKING"
    elif abs(velocity) <= v:
        phase = "STABLE"
    else:
        phase = "DECLINING"
    sig = "" if accel_for_phase else " (not significant)"
    return Momentum(phase, float(mult[phase]), velocity, acceleration, accel_t=accel_t,
                    reason=f"velocity {velocity:+.4f}/day, acceleration {acceleration:+.2f}{sig}",
                    **base)
