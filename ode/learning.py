"""Learn from YOUR results.

Every plan item has a stable id. When you start / publish / drop it and later
log views and revenue, the engine:
  * never suggests that topic+market+platform again (done filtering), and
  * calibrates what a visitor is really worth per market, and which platform
    really pays per hour for you — blended with the config priors so a single
    lucky video can't swing everything (Bayesian-style pseudo-counts).
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

BASE_RPV = 0.010  # ADS revenue per visitor that market_value 1.0 corresponds to


def action_id(topic_id: int | None, market: str, platform: str, topic: str = "") -> str:
    """Stable 8-char id for (topic, market, platform): same item = same id every day."""
    key = f"{topic_id if topic_id is not None else topic.casefold()}|{market}|{platform}"
    return hashlib.sha1(key.encode()).hexdigest()[:8]


@dataclass(frozen=True)
class WorkRow:
    market: str
    platform: str
    views: int | None
    revenue: float | None
    hours: float | None


def calibrate_market_values(rows: Iterable[WorkRow], priors: Mapping[str, float], *,
                            pseudo_views: float = 20_000.0) -> dict[str, float]:
    """Market value blended from the prior and your real revenue per visitor.

    The prior counts as `pseudo_views` views at its implied RPV, so a market
    needs real volume before your data dominates. Markets without data keep
    their prior. Result clipped to [0.02, 3.0].
    """
    views: dict[str, float] = defaultdict(float)
    revenue: dict[str, float] = defaultdict(float)
    for r in rows:
        if r.views and r.views > 0 and r.revenue is not None:
            views[r.market] += r.views
            revenue[r.market] += r.revenue
    out = dict(priors)
    for m, v in views.items():
        prior_rpv = priors.get(m, 0.5) * BASE_RPV
        blended = (prior_rpv * pseudo_views + revenue[m]) / (pseudo_views + v)
        out[m] = round(min(3.0, max(0.02, blended / BASE_RPV)), 3)
    return out


def calibrate_platforms(rows: Iterable[WorkRow], *, pseudo_items: float = 3.0
                        ) -> dict[str, float]:
    """Platform multiplier (around 1.0) from your revenue per hour (or per item
    when hours are missing), relative to your overall average. Clipped [0.5, 2]."""
    per: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r.revenue is None:
            continue
        per[r.platform].append(r.revenue / r.hours if r.hours else r.revenue)
    allv = [x for v in per.values() for x in v]
    if not allv:
        return {}
    overall = sum(allv) / len(allv)
    if overall <= 0:
        return {}
    out = {}
    for p, vals in per.items():
        ratio = (sum(vals) / len(vals)) / overall
        n = len(vals)
        blended = (1.0 * pseudo_items + ratio * n) / (pseudo_items + n)
        out[p] = round(min(2.0, max(0.5, blended)), 3)
    return out
