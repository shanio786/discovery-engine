"""Score components. Pure functions — no I/O — so every rule is unit-tested.

    DEMAND       0-100   how many people want this (log-normalized per market)
    MONEY        0-100   what a visitor is worth via SPONSOR / PRODUCT / APP / ADS
    GAP          0-100   how under-served it is (outliers, no dominant channel,
                         unanswered questions)
    COMPETITION  1-100   how much competing content exists (last 180 days)
    COMPOSITE            momentum x weighted geometric mean of the above
    CONFIDENCE   0-1     how much real data stands behind the number
    SATURATION           OPEN / NARROWING / CLOSING_FAST / UNKNOWN
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np

NEUTRAL = 50.0


# ------------------------------------------------------------------ normalization
def log_normalize(values: Mapping[str, float]) -> dict[str, float]:
    """{id: raw} -> {id: 0-100}: half log-scale with robust (p2..p98) bounds,
    half rank percentile.

    Per-corpus, so German topics are compared with German topics. A single
    giant article can't squash everyone else to ~0, and the top doesn't tie.
    """
    if not values:
        return {}
    keys = list(values)
    logs = np.log1p(np.maximum(np.array([values[k] for k in keys], dtype=float), 0.0))
    lo, hi = np.percentile(logs, 2), np.percentile(logs, 98)
    if hi - lo < 1e-9:
        return dict.fromkeys(keys, NEUTRAL)
    scaled = np.clip((logs - lo) / (hi - lo), 0.0, 1.0) * 100.0
    # Blend with the rank percentile so the clipped top 2% don't all tie at 100:
    # the best topics are exactly the ones that most need telling apart.
    order = logs.argsort(kind="stable")
    ranks = np.empty(len(logs))
    ranks[order] = np.arange(len(logs))
    for v in np.unique(logs):  # equal values share their average rank
        ranks[logs == v] = ranks[logs == v].mean()
    pct = ranks / max(1, len(logs) - 1) * 100.0
    blended = 0.5 * scaled + 0.5 * pct
    return {k: float(v) for k, v in zip(keys, blended, strict=True)}


def weighted_mean(parts: Mapping[str, float | None], weights: Mapping[str, float]) -> float | None:
    """Mean of the available parts, weights renormalized over what exists."""
    avail = {k: v for k, v in parts.items() if v is not None and weights.get(k, 0) > 0}
    if not avail:
        return None
    total = sum(weights[k] for k in avail)
    return sum(v * weights[k] for k, v in avail.items()) / total


# ------------------------------------------------------------------ money
def _has(text: str, phrase: str) -> bool:
    return re.search(rf"\b{re.escape(phrase.strip())}\b", text, re.IGNORECASE) is not None


@dataclass(frozen=True)
class Money:
    primary: str
    est_rpv: float
    score: float
    paths: dict[str, str] = field(default_factory=dict)   # path -> why it's eligible
    comparison_penalty: bool = False


def money_profile(
    texts: Iterable[str],
    *,
    kind: str | None,
    audience: str | None,
    config: Mapping,
) -> Money:
    """Eligible monetization paths and blended revenue-per-visitor.

    ADS is always available. PRODUCT / APP trigger on intent keywords in the
    topic's titles/queries; SPONSOR on a B2B audience (seed `audience: b2b`)
    or a professional Wikidata class. Comparison/review intent (old affiliate
    territory) is deprioritised. CPC-based SPONSOR triggers arrive with the
    Phase-4 verify layer.
    """
    rpv: dict[str, float] = {"SPONSOR": 0.25, "PRODUCT": 0.045, "APP": 0.030, "ADS": 0.010}
    rpv.update({k.upper(): float(v) for k, v in (config.get("rpv") or {}).items()})
    blob = " ".join(t for t in texts if t)
    paths: dict[str, str] = {"ADS": "always available"}
    for kw in config.get("product_keywords", []):
        if _has(blob, kw):
            paths.setdefault("PRODUCT", f"intent keyword '{kw.strip()}'")
            break
    for kw in config.get("app_keywords", []):
        if _has(blob, kw):
            paths.setdefault("APP", f"utility keyword '{kw.strip()}'")
            break
    if audience == "b2b":
        paths.setdefault("SPONSOR", "B2B / professional audience (seed)")
        paths.setdefault("PRODUCT", "B2B audience buys templates/courses")
    if kind:
        for path in (config.get("class_paths") or {}).get(kind, []):
            paths.setdefault(str(path).upper(), f"Wikidata class {kind}")

    ordered = sorted(paths, key=lambda p: rpv.get(p, 0.0), reverse=True)
    primary = ordered[0]
    secondary = float(config.get("secondary_weight", 0.2))
    blended = rpv[primary] + secondary * sum(rpv.get(p, 0.0) for p in ordered[1:])
    penalty = any(_has(blob, kw) for kw in config.get("comparison_keywords", []))
    if penalty:
        blended *= float(config.get("comparison_penalty", 0.7))

    ads = rpv["ADS"]
    best = rpv["SPONSOR"] + secondary * (rpv["PRODUCT"] + rpv["APP"] + rpv["ADS"])
    score = 100.0 * math.log1p(blended / ads) / math.log1p(best / ads)
    return Money(primary, round(blended, 5), round(min(100.0, max(0.0, score)), 2), paths, penalty)


# ------------------------------------------------------------------ gap
def gap_score(
    *,
    max_outlier: float | None,
    top_channel_share: float | None,
    unanswered_share: float | None,
    outlier_cap: float = 50.0,
    dominant_share: float = 0.30,
) -> tuple[float | None, dict[str, float]]:
    """0-100: small channels massively outperforming, nobody owning the topic,
    questions left unanswered. None when no gap data exists at all."""
    parts: dict[str, float] = {}
    if max_outlier is not None:
        parts["outlier"] = 100.0 * min(1.0, math.log1p(max(0.0, max_outlier))
                                       / math.log1p(outlier_cap))
    if top_channel_share is not None:
        share = min(1.0, max(0.0, top_channel_share))
        parts["no_dominant_channel"] = (
            100.0 if share <= dominant_share else 100.0 * (1.0 - share) / (1.0 - dominant_share)
        )
    if unanswered_share is not None:
        parts["unanswered"] = 100.0 * min(1.0, max(0.0, unanswered_share))
    if not parts:
        return None, {}
    return sum(parts.values()) / len(parts), parts


# ------------------------------------------------------------------ composite
def composite_score(
    *,
    multiplier: float,
    demand: float,
    money: float,
    gap: float | None,
    competition: float | None,
    weights: Mapping[str, float] | None = None,
) -> float:
    """momentum x weighted geometric mean(demand, money, gap, 101 - competition).

    The spec's (D x M x G) / C, but bounded and zero-safe: a missing gap or
    competition counts as neutral, and no single factor can zero everything.
    """
    w = {"demand": 1.0, "money": 1.0, "gap": 1.0, "competition": 1.0, **(weights or {})}
    parts = {
        "demand": demand,
        "money": money,
        "gap": NEUTRAL if gap is None else gap,
        "competition": 101.0 - (NEUTRAL if competition is None else competition),
    }
    total = sum(w[k] for k in parts)
    geo = math.exp(sum(w[k] * math.log(max(1.0, v)) for k, v in parts.items()) / total)
    return round(multiplier * geo, 3)


def confidence_score(*, points: int, phase: str, demand_sources: int, has_gap: bool,
                     has_competition: bool, money_specific: bool) -> float:
    """0-1: history length (40%) + how many components rest on real data (60%)."""
    history = min(1.0, points / 365.0)
    coverage = (min(demand_sources, 2) / 2 + has_gap + has_competition + money_specific) / 4
    conf = 0.4 * history + 0.6 * coverage
    if phase == "UNKNOWN":
        conf = min(conf, 0.3)
    return round(conf, 2)


# ------------------------------------------------------------------ saturation
def completed_months(as_of: date, n: int) -> list[date]:
    """Month starts of the last n COMPLETED months, newest first."""
    y, m = as_of.year, as_of.month
    out: list[date] = []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append(date(y, m, 1))
    return out


def saturation_forecast(monthly_uploads: Mapping[date, float], as_of: date) -> tuple[str, int | None]:
    """Spec: growth of new entrants, last 3 months vs months 10-12 ago.

    Needs all six of those completed months; otherwise UNKNOWN (never guess).
    Returns (label, approx months until the window closes).
    """
    months = completed_months(as_of, 12)
    recent, old = months[:3], months[9:12]
    if any(m not in monthly_uploads for m in recent + old):
        return "UNKNOWN", None
    r = sum(monthly_uploads[m] for m in recent) / 3
    o = sum(monthly_uploads[m] for m in old) / 3
    growth = (r - o) / (o + 1)
    if growth > 1.5:
        return "CLOSING_FAST", 2
    if growth > 0.5:
        return "NARROWING", 6
    return "OPEN", 12


def dominant_share(channel_ids: Sequence[str | None]) -> float | None:
    """Share of the most common channel among a topic's top videos."""
    ids = [c for c in channel_ids if c]
    if len(ids) < 3:
        return None
    _, counts = np.unique(np.array(ids), return_counts=True)
    return float(counts.max() / len(ids))
