"""Dollar estimate: model ranges, uncertainty combination, personal blending."""

import math
from datetime import UTC, datetime, timedelta

import pytest

from ode.scoring.actions import plan_topic
from ode.scoring.dollars import (
    Dollars,
    Personal,
    _nice,
    blend,
    combine,
    estimate,
    model_range,
    personal_stats,
)
from tests.test_actions import view


def test_combine_is_narrower_than_worst_times_best():
    lo, hi = combine(100.0, [(1, 4), (2, 8)])
    assert lo > 100 * 1 * 2 and hi < 100 * 4 * 8              # not extreme x extreme
    assert math.isclose(math.sqrt(lo * hi), 100 * 2 * 4)      # centre = geometric means
    assert combine(5.0, []) == pytest.approx((5.0, 5.0))


def kw(**over):
    base = {"wiki_daily": 1000.0, "google": {}, "phase": "STABLE", "market_value": 1.0,
            "tutorial_mult": 1.0, "ease": 50.0, "cross": False, "tool_intent": True}
    return {**base, **over}


def mid(r):
    return math.sqrt(r[0] * r[1])


def test_model_moves_the_right_way():
    base = mid(model_range("BLOG", **kw()))
    assert mid(model_range("BLOG", **kw(ease=90))) > base > mid(model_range("BLOG", **kw(ease=20)))
    assert mid(model_range("BLOG", **kw(market_value=0.1))) == pytest.approx(base * 0.1, rel=1e-6)
    assert mid(model_range("BLOG", **kw(phase="BREAKOUT"))) > base > mid(
        model_range("BLOG", **kw(phase="DECLINING")))
    assert mid(model_range("BLOG", **kw(tutorial_mult=1.5))) > base
    assert model_range("BLOG", **kw(wiki_daily=None)) == (0.0, 0.0)
    cross = model_range("BLOG", **kw(cross=True))
    assert cross[1] / cross[0] > model_range("BLOG", **kw())[1] / model_range("BLOG", **kw())[0]
    # measured Google volume replaces the Wikipedia guess (and narrows the band)
    g = model_range("BLOG", **kw(google={"gsv": 30000.0, "gsv_longtail": 60000.0}))
    w = model_range("BLOG", **kw())
    assert g[1] / g[0] < w[1] / w[0]
    assert model_range("TOOLSITE", **kw(tool_intent=False))[0] < model_range("TOOLSITE", **kw())[0]
    s = model_range("SHORTS", **kw(wiki_daily=None))
    assert 0 < s[0] < s[1]                                       # shorts need no search demand


def test_personal_results_pull_the_estimate():
    model = (10.0, 100.0)
    assert blend(model, 12, None).basis == "model"
    me = Personal(monthly=(400.0, 500.0, 600.0, 800.0), hours=(4.0, 6.0))
    d = blend(model, 12, me)
    assert d.low > 10 and d.high > 100 and "4 results" in d.basis and d.hours == 5.0
    few = blend(model, 12, Personal(monthly=(900.0,), hours=()))
    assert few.basis == "model"                                  # one result is not enough


def test_personal_stats_from_worklog():
    now = datetime.now(UTC)
    rows = [{"platform": "YOUTUBE", "revenue_usd": 300.0, "hours": 10,
             "started_at": now - timedelta(days=90), "updated_at": now},
            {"platform": "YOUTUBE", "revenue_usd": 0.0, "hours": 6,
             "started_at": now - timedelta(days=10), "updated_at": now},
            {"platform": "BLOG", "revenue_usd": None, "hours": None,
             "started_at": now, "updated_at": now}]
    st = personal_stats(rows)
    assert st["YOUTUBE"].monthly == (100.0,) and st["YOUTUBE"].hours == (10.0, 6.0)
    assert "BLOG" not in st


def test_estimate_and_labels():
    t = view(google={})
    a = next(x for x in plan_topic(t) if x.platform == "BLOG")
    d = estimate(a, t, market_value=1.0, tutorial_mult=1.5)
    assert d.low < d.high and d.hours == 12 and d.per_hour == d.mid / 12
    assert Dollars(3.2, 1234.0, 5, "model").label() == "$3–$1,200/mahina"
    assert _nice(0.4) == "0.4" and _nice(56789) == "57,000"
