"""Dollar estimate per plan item: roughly $X-$Y per month once it matures
(3-6 months), and the hours it takes to set up.

A transparent model, not a promise:

    monthly demand   Google monthly searches (DataForSEO) when measured, else
                     Wikipedia views/day x 30 x searches-per-view; plus the
                     long tail (measured or a factor)
    growth           by phase (BREAKOUT x2 ... DECLINING x0.5)
    your share       a capture range per platform, scaled by ease
    value            RPM per platform x market value (calibrated from your
                     results) x the tutorial multiplier

Every range is a (low, high) pair from `dollars:` in config. Once you have
at least two monetized results on a platform, the range is pulled toward
YOUR real revenue per item per month (more results = more weight), and the
hours toward your own logged hours.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

GROWTH = {"BREAKOUT": 2.0, "GROWING": 1.4, "PEAKING": 0.8, "STABLE": 1.0, "DECLINING": 0.5,
          "UNKNOWN": 1.0}
DEFAULTS: dict[str, Any] = {
    "search_per_wiki_view": [1.0, 4.0],
    "longtail_factor": [1.5, 4.0],       # long-tail searches per head-term search
    "youtube_search_share": [0.3, 1.0],  # YouTube searches relative to Google
    "youtube_browse_factor": [1.5, 4.0], # total views per search view (suggested / browse)
    "cross_language_demand": [0.15, 0.6],
    "capture": {"BLOG": [0.02, 0.10], "YOUTUBE": [0.01, 0.06], "TOOLSITE": [0.02, 0.12],
                "APP": [0.002, 0.01]},
    "rpm_usd": {"BLOG": 10.0, "YOUTUBE": 3.0, "SHORTS": 0.06, "TOOLSITE": 8.0,
                "APP": 50.0},             # APP: per 1000 installs per month (AdMob)
    "shorts_views_per_short": [500, 8000],
    "shorts_per_month": 30,
    "effort_hours": {"BLOG": 12, "YOUTUBE": 8, "SHORTS": 5, "TOOLSITE": 10, "APP": 25},
    "min_personal_results": 2,
}


@dataclass(frozen=True)
class Dollars:
    low: float
    high: float
    hours: float
    basis: str                 # "model" or "model + N of your results"

    @property
    def mid(self) -> float:
        return math.sqrt(max(self.low, 0.01) * max(self.high, 0.01))

    @property
    def per_hour(self) -> float:
        return self.mid / max(self.hours, 0.5)

    def label(self) -> str:
        return f"${_nice(self.low)}–${_nice(self.high)}/mahina"


@dataclass(frozen=True)
class Personal:
    """Your monetized results on one platform."""
    monthly: tuple[float, ...]   # revenue per item per month
    hours: tuple[float, ...]


def _nice(x: float) -> str:
    if x < 10:
        return f"{x:.0f}" if x >= 1 else f"{x:.1f}".rstrip("0").rstrip(".") or "0"
    mag = 10 ** (len(str(int(x))) - 2)
    return f"{round(x / mag) * mag:,.0f}"


def _cfg(cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for k, v in (cfg or {}).items():
        out[k] = {**out.get(k, {}), **v} if isinstance(v, dict) else v
    return out


def combine(fixed: float, ranges: Iterable[tuple[float, float]]) -> tuple[float, float]:
    """Pure: product of a fixed factor and independent uncertain (low, high)
    factors. Uncertainties add in log space (root-sum-square) instead of
    multiplying every worst case and every best case together, which would
    give a uselessly wide band."""
    center, var = math.log(max(fixed, 1e-12)), 0.0
    for lo, hi in ranges:
        lo, hi = max(lo, 1e-12), max(hi, 1e-12)
        center += (math.log(lo) + math.log(hi)) / 2
        var += ((math.log(hi) - math.log(lo)) / 2) ** 2
    half = math.sqrt(var)
    return math.exp(center - half), math.exp(center + half)


def model_range(platform: str, *, wiki_daily: float | None, google: Mapping[str, float],
                phase: str, market_value: float, tutorial_mult: float, ease: float,
                cross: bool, tool_intent: bool, cfg: Mapping[str, Any] | None = None
                ) -> tuple[float, float]:
    """Pure: (low, high) USD per month at maturity for one action."""
    c = _cfg(cfg)
    ease_f = min(1.8, max(0.3, ease / 50.0))
    growth = GROWTH.get(phase, 1.0)
    rpm = float(c["rpm_usd"][platform]) * market_value / 1000.0
    if platform == "SHORTS":
        return combine(float(c["shorts_per_month"]) * ease_f * growth * rpm,
                       [tuple(c["shorts_views_per_short"])])
    ranges: list[tuple[float, float]] = []
    gsv = None if cross else google.get("gsv")
    if gsv is not None:
        fixed = float(gsv)
    elif wiki_daily:
        fixed = wiki_daily * 30
        ranges.append(tuple(c["search_per_wiki_view"]))
    else:
        return (0.0, 0.0)
    tail = None if cross else google.get("gsv_longtail")
    if tail and gsv is not None:
        fixed += float(tail)
    else:
        ranges.append(tuple(c["longtail_factor"]))
    if cross:
        ranges.append(tuple(c["cross_language_demand"]))
    ranges.append(tuple(c["capture"].get(platform, [0.01, 0.05])))
    if platform == "YOUTUBE":
        ranges += [tuple(c["youtube_search_share"]), tuple(c["youtube_browse_factor"])]
    fixed *= ease_f * growth * rpm * (tutorial_mult if platform in ("BLOG", "YOUTUBE",
                                                                    "TOOLSITE") else 1.0)
    if platform == "TOOLSITE" and not tool_intent:
        fixed *= 0.5
    return combine(fixed, ranges)


def personal_stats(rows: Iterable[Mapping[str, Any]]) -> dict[str, Personal]:
    """Per platform: revenue per item per month and hours, from monetized results.
    Rows need platform, revenue_usd, hours, started_at, updated_at."""
    monthly: dict[str, list[float]] = {}
    hours: dict[str, list[float]] = {}
    for r in rows:
        rev = r.get("revenue_usd")
        if r.get("hours"):
            hours.setdefault(r["platform"], []).append(float(r["hours"]))
        if not rev or rev <= 0:
            continue
        months = max(1.0, (r["updated_at"] - r["started_at"]).days / 30.0)
        monthly.setdefault(r["platform"], []).append(float(rev) / months)
    return {p: Personal(tuple(monthly.get(p, ())), tuple(hours.get(p, ())))
            for p in set(monthly) | set(hours)}


def _quartiles(xs: list[float]) -> tuple[float, float]:
    xs = sorted(xs)
    if len(xs) < 4:
        return xs[0], xs[-1]
    q = statistics.quantiles(xs, n=4)
    return q[0], q[2]


def blend(model: tuple[float, float], hours: float, me: Personal | None,
          min_results: int = 2) -> Dollars:
    """Pull the model toward your own results (log space; weight n/(n+3))."""
    lo, hi = model
    basis = "model"
    if me and len(me.monthly) >= min_results:
        n = len(me.monthly)
        w = n / (n + 3.0)
        p_lo, p_hi = _quartiles(list(me.monthly))

        def mix(a: float, b: float) -> float:
            return math.exp((1 - w) * math.log(max(a, 0.01)) + w * math.log(max(b, 0.01)))
        lo, hi = mix(lo, p_lo), mix(hi, p_hi)
        basis = f"model + tumhare {n} results"
    if me and len(me.hours) >= min_results:
        hours = statistics.median(me.hours)
    return Dollars(round(lo, 2), round(max(hi, lo), 2), float(hours), basis)


def estimate(a: Any, t: Any, *, market_value: float, tutorial_mult: float,
             personal: Mapping[str, Personal] | None = None,
             cfg: Mapping[str, Any] | None = None) -> Dollars:
    """Dollars for action `a` built from topic view `t`."""
    c = _cfg(cfg)
    tool_intent = bool(getattr(t, "tool_keyword", None)) or "APP" in (t.money_paths or {})
    phase = "STABLE" if t.seasonal else t.phase
    model = model_range(a.platform, wiki_daily=t.mean30, google=t.google or {}, phase=phase,
                        market_value=market_value,
                        tutorial_mult=tutorial_mult if t.tutorial else 1.0, ease=a.ease,
                        cross=bool(a.source_market), tool_intent=tool_intent, cfg=c)
    hours = float(c["effort_hours"].get(a.platform, 10))
    return blend(model, hours, (personal or {}).get(a.platform),
                 int(c["min_personal_results"]))
