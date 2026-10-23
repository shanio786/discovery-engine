"""Backtest: did the topics the scorer ranked highest really grow?

For each cut-off date (e.g. 30, 60 and 90 days before the newest data) the
REAL scorer runs on data up to that date only (`MetricReader` makes
lookahead impossible). Then we look at what happened next:

    grew  =  mean daily views in the `horizon` days after the cut-off
             >= `growth_ratio` x mean of the 30 days before it

    precision@k  share of the top-k (pooled over markets) that grew
    base rate    share of ALL evaluated topics that grew = what random picks get
    lift         precision@k / base rate  (> 1 means the ranking beats chance)

Universe bias: topics admitted to the watchlist BECAUSE they later became
popular (top lists, trends, spikes) would flatter the scorer. Only topics
that were already tracked at the cut-off, or were admitted for reasons
unrelated to popularity (seeds, "more like this"), are evaluated.

Weight learning: the component weights of the composite are grid-searched
on the stored samples (cheap, in memory). New weights are adopted only when
they beat the current ones on average AND on most cut-offs by a margin, so
noise can't wander the weights around. `money` is never tuned here: the
backtest measures growth, not what a visitor earns.
"""

from __future__ import annotations

import itertools
import json
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ode.config import Config
from ode.db import Database
from ode.logging import get_logger
from ode.scoring.components import composite_score

log = get_logger(__name__)

SAFE_ORIGINS = ("seed", "morelike")
WEIGHTS_KEY = "composite_weights"
DEFAULT_GRID = {"momentum": [0.5, 1.0, 1.5, 2.0], "demand": [0.5, 1.0, 2.0],
                "gap": [0.5, 1.0, 2.0], "competition": [0.5, 1.0, 2.0]}


@dataclass(frozen=True)
class Sample:
    market: str
    topic_id: int
    multiplier: float
    demand: float
    money: float
    gap: float | None
    competition: float | None
    grew: bool
    ratio: float


def growth_ratio(series: Mapping[date, float], cutoff: date, horizon: int, *,
                 min_days: int = 20, min_base: float = 5.0) -> float | None:
    """Pure: future mean / past-30 mean, or None when either side is too thin."""
    before = [v for d, v in series.items() if cutoff - timedelta(days=30) < d <= cutoff]
    after = [v for d, v in series.items() if cutoff < d <= cutoff + timedelta(days=horizon)]
    if len(before) < min_days or len(after) < min(min_days, horizon):
        return None
    base = statistics.fmean(before)
    if base < min_base:
        return None
    return statistics.fmean(after) / base


def score(s: Sample, weights: Mapping[str, float]) -> float:
    return composite_score(multiplier=s.multiplier, demand=s.demand, money=s.money, gap=s.gap,
                           competition=s.competition, weights=weights)


def evaluate(samples: list[Sample], weights: Mapping[str, float], k: int) -> dict[str, Any]:
    """Pure: precision@k, base rate and lift for one cut-off's samples."""
    n = len(samples)
    if not n:
        return {"universe": 0, "k": k, "hits": 0, "base_rate": 0.0, "precision": 0.0,
                "lift": None}
    ranked = sorted(samples, key=lambda s: (score(s, weights), s.topic_id), reverse=True)
    top = ranked[: min(k, n)]
    hits = sum(s.grew for s in top)
    base = sum(s.grew for s in samples) / n
    precision = hits / len(top)
    return {"universe": n, "k": len(top), "hits": hits, "base_rate": round(base, 4),
            "precision": round(precision, 4),
            "lift": round(precision / base, 3) if base > 0 else None}


def grid_search(by_cutoff: Mapping[date, list[Sample]], current: Mapping[str, float], k: int,
                grid: Mapping[str, Iterable[float]] | None = None, *,
                min_improvement: float = 0.05) -> dict[str, Any]:
    """Pure: best weights over the grid by mean lift across cut-offs, and
    whether they should replace `current` (beat it by `min_improvement` on
    average AND on a majority of cut-offs)."""
    grid = {**DEFAULT_GRID, **(grid or {})}
    keys = list(grid)

    def mean_lift(w: Mapping[str, float]) -> tuple[float, list[float]]:
        lifts = [evaluate(s, w, k)["lift"] or 0.0 for s in by_cutoff.values()]
        return (statistics.fmean(lifts) if lifts else 0.0), lifts

    cur_mean, cur_lifts = mean_lift(current)
    best_w, best_mean, best_lifts = dict(current), cur_mean, cur_lifts
    for combo in itertools.product(*(grid[key] for key in keys)):
        w = {**current, **dict(zip(keys, combo, strict=True))}
        m, lifts = mean_lift(w)
        if m > best_mean + 1e-9:
            best_w, best_mean, best_lifts = w, m, lifts
    wins = sum(b > c + 1e-9 for b, c in zip(best_lifts, cur_lifts, strict=True))
    adopt = (best_w != dict(current) and best_mean >= cur_mean * (1 + min_improvement)
             and wins > len(cur_lifts) / 2)
    return {"weights": best_w, "mean_lift": round(best_mean, 3),
            "current_mean_lift": round(cur_mean, 3), "wins": wins, "adopt": adopt}


async def collect_samples(db: Database, config: Config, cutoff: date, horizon: int,
                          threshold: float) -> list[Sample]:
    """Score every market as of `cutoff` with the real scorer; attach outcomes."""
    from ode.scoring.scorer import Scorer

    scorer = Scorer(config, db)
    out: list[Sample] = []
    for market in config.markets():
        scored = [t for t in await scorer.score_market(market, cutoff)
                  if t.momentum.phase != "UNKNOWN"]
        if not scored:
            continue
        safe = {r["id"] for r in await db.pool.fetch(
            """
            SELECT DISTINCT t.id FROM topics t JOIN watchlist w ON w.topic_key = t.topic_key
            WHERE w.market = $1 AND t.id = ANY($2)
              AND (w.first_seen <= $3 OR w.origin = ANY($4))
            """, market.code, [t.topic_id for t in scored], cutoff, list(SAFE_ORIGINS))}
        ids = [t.topic_id for t in scored if t.topic_id in safe]
        rows = await db.pool.fetch(
            "SELECT topic_id, date, value FROM topic_metrics WHERE market = $1 "
            "AND source = 'wikipedia' AND metric_name = 'pageviews' AND topic_id = ANY($2) "
            "AND date > $3 AND date <= $4",
            market.code, ids, cutoff - timedelta(days=30), cutoff + timedelta(days=horizon))
        series: dict[int, dict[date, float]] = defaultdict(dict)
        for r in rows:
            series[r["topic_id"]][r["date"]] = r["value"]
        for t in scored:
            if t.topic_id not in safe:
                continue
            ratio = growth_ratio(series.get(t.topic_id, {}), cutoff, horizon)
            if ratio is None:
                continue
            out.append(Sample(market.code, t.topic_id, t.momentum.multiplier, t.demand,
                              t.money.score, t.gap, t.competition, ratio >= threshold,
                              round(ratio, 3)))
    return out


async def latest_data_day(db: Database) -> date | None:
    return await db.pool.fetchval(
        "SELECT max(date) FROM topic_metrics WHERE source = 'wikipedia' "
        "AND metric_name = 'pageviews'")


def cutoffs_for(latest: date, horizon: int, offsets: Iterable[int]) -> list[date]:
    """Cut-offs whose whole outcome window is already in the data."""
    return [latest - timedelta(days=horizon + off) for off in offsets]


async def run_backtest(config: Config, db: Database, *, persist: bool = True) -> dict[str, Any]:
    cfg = config.backtest
    horizon = int(cfg.get("horizon_days", 30))
    k = int(cfg.get("k", 20))
    threshold = float(cfg.get("growth_ratio", 1.5))
    min_universe = int(cfg.get("min_universe", 50))
    latest = await latest_data_day(db)
    if latest is None:
        return {"status": "skipped", "reason": "no Wikipedia data yet"}
    current = {"demand": 1.0, "money": 1.0, "gap": 1.0, "competition": 1.0, "momentum": 1.0,
               **(config.scoring.get("composite_weights") or {})}
    by_cutoff: dict[date, list[Sample]] = {}
    for cutoff in cutoffs_for(latest, horizon, cfg.get("cutoff_offsets_days", [0, 30, 60])):
        samples = await collect_samples(db, config, cutoff, horizon, threshold)
        if len(samples) >= min_universe:
            by_cutoff[cutoff] = samples
        else:
            log.info("backtest.cutoff_too_small", cutoff=str(cutoff), universe=len(samples))
    if not by_cutoff:
        return {"status": "skipped",
                "reason": f"fewer than {min_universe} evaluable topics at every cut-off "
                          "(needs ~60 days of history after the first collect)"}
    results = {c: evaluate(s, current, k) for c, s in by_cutoff.items()}
    search = grid_search(by_cutoff, current, k, cfg.get("grid"),
                         min_improvement=float(cfg.get("min_improvement", 0.05)))
    if persist:
        run_at = datetime.now(UTC)  # one timestamp = one run, for the report header
        for c, r in results.items():
            await db.pool.execute(
                """
                INSERT INTO backtest_runs (run_at, cutoff, horizon, market, universe, k, hits,
                                       base_rate, precision_k, lift, weights)
                VALUES ($1, $2, $3, NULL, $4, $5, $6, $7, $8, $9, $10::jsonb)
                """, run_at, c, horizon, r["universe"], r["k"], r["hits"], r["base_rate"],
                r["precision"], r["lift"], json.dumps(current))
        if cfg.get("learn_weights", True):
            value = {"weights": search["weights"] if search["adopt"] else current,
                     "adopted": search["adopt"], "mean_lift": search["mean_lift"],
                     "baseline_mean_lift": search["current_mean_lift"],
                     "cutoffs": [str(c) for c in by_cutoff]}
            await db.pool.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2::jsonb, now()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                WEIGHTS_KEY, json.dumps(value))
    lifts = [r["lift"] for r in results.values() if r["lift"] is not None]
    out = {"status": "ok", "cutoffs": len(results), "k": k, "horizon": horizon,
           "precision": round(statistics.fmean(r["precision"] for r in results.values()), 3),
           "base_rate": round(statistics.fmean(r["base_rate"] for r in results.values()), 3),
           "lift": round(statistics.fmean(lifts), 2) if lifts else None,
           "weights_adopted": search["adopt"], "learned_lift": search["mean_lift"],
           "per_cutoff": {str(c): r for c, r in results.items()}}
    log.info("backtest.done", **{k_: v for k_, v in out.items() if k_ != "per_cutoff"})
    return out


async def learned_weights(db: Database, config: Config) -> dict[str, float] | None:
    """Adopted backtest weights, or None (not adopted / disabled / never run)."""
    if not config.backtest.get("apply_learned_weights", True):
        return None
    raw = await db.pool.fetchval("SELECT value FROM settings WHERE key = $1", WEIGHTS_KEY)
    if raw is None:
        return None
    value = raw if isinstance(raw, dict) else json.loads(raw)
    if not value.get("adopted"):
        return None
    return {k: float(v) for k, v in (value.get("weights") or {}).items()}


async def validation_note(db: Database) -> str:
    """Report header: the latest backtest's numbers, or the UNVALIDATED warning."""
    from ode.scoring.report import UNVALIDATED

    rows = await db.pool.fetch(
        "SELECT cutoff, k, precision_k, base_rate, lift, run_at FROM backtest_runs "
        "WHERE market IS NULL AND run_at = (SELECT max(run_at) FROM backtest_runs "
        "WHERE market IS NULL) ORDER BY cutoff")
    if not rows:
        return UNVALIDATED
    prec = statistics.fmean(r["precision_k"] for r in rows)
    base = statistics.fmean(r["base_rate"] for r in rows)
    lifts = [r["lift"] for r in rows if r["lift"] is not None]
    lift = statistics.fmean(lifts) if lifts else None
    verdict = ("ranking beats random picks" if lift and lift > 1.2 else
               "ranking is NOT clearly better than random yet — verify before investing time")
    return (f"Backtest {rows[-1]['run_at']:%Y-%m-%d} ({len(rows)} cut-offs): of the top "
            f"{rows[0]['k']}, {prec:.0%} really grew vs {base:.0%} for random picks"
            + (f" (lift {lift:.1f}x)" if lift else "") + f" — {verdict}.")
