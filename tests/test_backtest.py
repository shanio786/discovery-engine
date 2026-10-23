"""Backtest: outcomes, precision@k vs random, bias-safe universe, weight learning."""

import math
from datetime import date, timedelta

import numpy as np

from ode import backtest
from ode.backtest import Sample, evaluate, grid_search, growth_ratio
from ode.scoring.report import UNVALIDATED
from ode.scoring.scorer import Scorer
from tests.conftest import make_config
from tests.test_plan_briefs import cfg as plan_cfg
from tests.test_scoring_db import wiki

END = date(2026, 6, 30)
D0 = date(2026, 1, 1)


def series(values, end=D0):
    return {end - timedelta(days=len(values) - 1 - i): v for i, v in enumerate(values)}


def test_growth_ratio():
    s = series([100.0] * 30 + [200.0] * 30, end=D0 + timedelta(days=30))
    assert growth_ratio(s, D0, 30) == 2.0
    assert growth_ratio(series([100.0] * 10, end=D0), D0, 30) is None        # thin past
    assert growth_ratio(series([1.0] * 60, end=D0 + timedelta(days=30)), D0, 30) is None  # tiny


def sample(i, grew, mult=1.0, demand=50.0, gap=None, comp=None):
    return Sample("en-US", i, mult, demand, 50.0, gap, comp, grew, 2.0 if grew else 1.0)


def test_precision_and_lift_vs_random():
    samples = [sample(i, grew=i < 5, mult=3.0 if i < 5 else 1.0) for i in range(20)]
    r = evaluate(samples, {}, k=5)
    assert r == {"universe": 20, "k": 5, "hits": 5, "base_rate": 0.25, "precision": 1.0,
                 "lift": 4.0}
    assert evaluate([], {}, 5)["lift"] is None
    assert evaluate([sample(1, False)], {}, 5)["lift"] is None               # nobody grew


def test_grid_search_adopts_only_clear_improvements():
    # growth follows demand here, while momentum misleads: more demand weight must win
    def world(seed):
        rng = np.random.default_rng(seed)
        out = []
        for i in range(40):
            demand = float(rng.uniform(5, 100))
            mult = float(rng.choice([0.6, 1.0, 1.8, 3.0]))
            out.append(sample(i, grew=demand > 70, mult=mult, demand=demand))
        return out
    by_cutoff = {date(2026, 1, d): world(d) for d in (1, 2, 3)}
    cur = {"demand": 1.0, "money": 1.0, "gap": 1.0, "competition": 1.0, "momentum": 1.0}
    res = grid_search(by_cutoff, cur, k=8)
    assert res["adopt"] and res["mean_lift"] > res["current_mean_lift"]
    assert res["weights"]["momentum"] <= 0.5 and res["weights"]["money"] == 1.0
    # nothing to gain -> keep current
    flat = {date(2026, 1, 1): [sample(i, grew=i % 2 == 0) for i in range(20)]}
    assert not grid_search(flat, cur, k=5)["adopt"]


def bt_config(**over):
    base = plan_cfg()
    raw = base.raw()
    raw["markets"] = ["en-US"]
    raw["backtest"] = {"horizon_days": 30, "cutoff_offsets_days": [0, 30], "k": 5,
                       "growth_ratio": 1.5, "min_universe": 5, **over}
    return make_config(**raw)


async def backtest_world(db):
    i = np.arange(300, dtype=float)
    sigs = []
    keys = []
    for n in range(8):   # exponential growers: ~1.8x per 30 days, keep growing
        keys.append(f"wd:Q{100 + n}")
        sigs += wiki(keys[-1], "en-US", f"Grower {n}", 20 * np.exp(0.02 * i) + n, end=END)
    for n in range(12):  # flat and declining
        keys.append(f"wd:Q{200 + n}")
        vals = (800 + 5 * n) * np.exp(-0.004 * i) if n % 2 else np.full(300, 600.0 + n)
        sigs += wiki(keys[-1], "en-US", f"Steady {n}", vals, end=END)
    # admitted late BECAUSE it got popular (would flatter the scorer): excluded
    keys.append("wd:Q999")
    sigs += wiki(keys[-1], "en-US", "Late hit", 20 * np.exp(0.02 * i), end=END)
    await db.upsert_signals(sigs)
    for k in keys:
        await db.watchlist_upsert("en-US", "wiki_article", k, k, "wiki_top")
    await db.pool.execute("UPDATE watchlist SET first_seen = $1 WHERE topic_key <> 'wd:Q999'",
                          END - timedelta(days=200))
    await db.pool.execute("UPDATE watchlist SET first_seen = $1 WHERE topic_key = 'wd:Q999'",
                          END)


async def test_backtest_end_to_end(db):
    await backtest_world(db)
    config = bt_config()
    r = await backtest.run_backtest(config, db)
    assert r["status"] == "ok" and r["cutoffs"] == 2
    for x in r["per_cutoff"].values():
        assert x["universe"] == 20                       # the late hit is not evaluated
        assert x["base_rate"] == 0.4 and x["precision"] == 1.0
        assert math.isclose(x["lift"], 2.5)
    assert await db.pool.fetchval("SELECT count(*) FROM backtest_runs") == 2
    note = await backtest.validation_note(db)
    assert "100% really grew vs 40% for random picks" in note and "lift 2.5x" in note
    assert "beats random" in note
    # perfect already: nothing adopted, scorer keeps config weights
    assert await backtest.learned_weights(db, config) is None
    assert (await Scorer(config, db).load_weights()) == config.scoring["composite_weights"]


async def test_backtest_skips_without_enough_history(db):
    assert (await backtest.run_backtest(bt_config(), db))["status"] == "skipped"
    await db.upsert_signals(wiki("wd:Q1", "en-US", "x", [100.0] * 50, end=END))
    r = await backtest.run_backtest(bt_config(), db)
    assert r["status"] == "skipped" and "fewer than 5" in r["reason"]


async def test_adopted_weights_reach_the_scorer(db):
    config = bt_config()
    await db.pool.execute(
        "INSERT INTO settings (key, value) VALUES ('composite_weights', $1::jsonb)",
        '{"weights": {"demand": 2.0, "momentum": 0.5}, "adopted": true}')
    assert await backtest.learned_weights(db, config) == {"demand": 2.0, "momentum": 0.5}
    assert (await Scorer(config, db).load_weights())["demand"] == 2.0
    off = bt_config(apply_learned_weights=False)
    assert await backtest.learned_weights(db, off) is None
    assert await backtest.validation_note(db) == UNVALIDATED          # never ran
