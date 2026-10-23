"""Seasonal peaks: topics that come back every year (exams, tax, holidays,
Ramadan, summer gadgets …). With ~13 months of daily history we know which
calendar month a topic peaks in, so the plan can say "start now" weeks BEFORE
the peak — AI content still needs time to rank.
"""

from __future__ import annotations

import calendar
import statistics
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Seasonal:
    peak_month: int
    ratio: float          # peak month mean / median month mean
    weeks_until: int      # weeks from as_of to the start of the next peak month

    @property
    def month_name(self) -> str:
        return calendar.month_name[self.peak_month]

    def as_dict(self) -> dict:
        return {"month": self.peak_month, "month_name": self.month_name,
                "ratio": round(self.ratio, 1), "weeks": self.weeks_until}


def seasonal_peak(series: Mapping[date, float], as_of: date, *, min_history_days: int = 365,
                  peak_ratio: float = 2.0, lead_weeks: tuple[int, int] = (2, 8)
                  ) -> Seasonal | None:
    """The topic's yearly peak month if it starts within `lead_weeks` of as_of.

    Uses only the last 365 days up to as_of (no lookahead), one mean per
    calendar month (months with < 20 days of data are ignored), and calls a
    month a peak when it is >= peak_ratio x the median month.
    """
    obs = {d: v for d, v in series.items() if d <= as_of and v is not None}
    if not obs or (as_of - min(obs)).days < min_history_days:
        return None
    start = date(as_of.year - 1, as_of.month, 1)
    buckets: dict[int, list[float]] = defaultdict(list)
    for d, v in obs.items():
        if d >= start:
            buckets[d.month].append(float(v))
    means = {m: sum(v) / len(v) for m, v in buckets.items() if len(v) >= 20}
    if len(means) < 10:
        return None
    median = statistics.median(means.values())
    if median <= 0:
        return None
    peak_month, peak_mean = max(means.items(), key=lambda kv: kv[1])
    ratio = peak_mean / median
    if ratio < peak_ratio:
        return None
    year = as_of.year if peak_month > as_of.month else as_of.year + 1
    weeks = (date(year, peak_month, 1) - as_of).days // 7
    lo, hi = lead_weeks
    if not lo <= weeks <= hi:
        return None
    return Seasonal(peak_month, ratio, weeks)
