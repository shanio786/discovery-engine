"""Work log: stable ids, done filtering, learning from results."""

import pytest

from ode import worklog
from ode.learning import WorkRow, action_id, calibrate_market_values, calibrate_platforms
from ode.scoring import plan as planmod
from ode.scoring.scorer import Scorer
from tests.test_plan_briefs import cfg
from tests.test_scoring_db import AS_OF, seed_world


def test_action_id_is_stable_and_distinct():
    assert action_id(7, "de-DE", "BLOG") == action_id(7, "de-DE", "BLOG")
    assert len(action_id(7, "de-DE", "BLOG")) == 8
    assert action_id(7, "de-DE", "BLOG") != action_id(7, "de-DE", "YOUTUBE")
    assert action_id(None, "de-DE", "BLOG", "Foo") == action_id(None, "de-DE", "BLOG", "foo")


def test_market_calibration_is_damped_by_prior():
    priors = {"en-US": 1.0, "ur-PK": 0.08}
    # one small lucky result barely moves the prior ...
    small = calibrate_market_values([WorkRow("ur-PK", "YOUTUBE", 500, 5.0, 2)], priors)
    assert 0.08 < small["ur-PK"] < 0.4
    # ... lots of real volume dominates it
    big = calibrate_market_values([WorkRow("ur-PK", "YOUTUBE", 500_000, 2500.0, 40)], priors)
    assert big["ur-PK"] == pytest.approx(0.43, abs=0.02)
    assert big["en-US"] == 1.0                      # untouched without data
    assert calibrate_market_values([WorkRow("ur-PK", "BLOG", 0, 1.0, 1)], priors) == priors
    # not monetized yet ($0) says nothing about the market or the platform
    unpaid = [WorkRow("ur-PK", "YOUTUBE", 90_000, 0.0, 30)]
    assert calibrate_market_values(unpaid, priors) == priors
    assert calibrate_platforms(unpaid) == {}


def test_platform_calibration():
    rows = [WorkRow("en-US", "YOUTUBE", 1, 100.0, 10), WorkRow("en-US", "YOUTUBE", 1, 80.0, 8),
            WorkRow("en-US", "BLOG", 1, 10.0, 10), WorkRow("en-US", "BLOG", 1, 5.0, 5)]
    out = calibrate_platforms(rows)
    assert out["YOUTUBE"] > 1.0 > out["BLOG"]
    assert 0.5 <= out["BLOG"] and out["YOUTUBE"] <= 2.0
    assert calibrate_platforms([]) == {}


async def test_done_items_leave_the_plan_and_results_calibrate(db):
    await seed_world(db)
    config = cfg()
    await Scorer(config, db).run(AS_OF)
    plan = await planmod.build_plan(db, config, AS_OF)
    first = plan[0]
    assert await db.pool.fetchval("SELECT count(*) FROM plan_items") == len(plan)
    assert first.action_id in planmod.render_plan_markdown(plan, AS_OF)

    with pytest.raises(worklog.UnknownAction):
        await worklog.mark(db, "deadbeef")
    with pytest.raises(ValueError):
        await worklog.mark(db, first.action_id, "finished")
    r = await worklog.mark(db, first.action_id, hours=3, note="batch 1")
    assert r["status"] == "started" and r["topic"] == first.topic

    again = await planmod.build_plan(db, config, AS_OF)
    assert first.action_id not in {a.action_id for a in again}       # never suggested twice

    r = await worklog.record_result(db, first.action_id, views=400_000, revenue=8000.0)
    assert r["status"] == "published" and r["hours"] == 3 and r["note"] == "batch 1"
    values, plat = await worklog.calibration(db, config)
    assert values[first.market] > config.market_value(first.market)   # paid more than assumed
    assert plat == {first.platform: 1.0}                              # only one platform yet

    off = cfg()
    off.raw()["learning"] = {"enabled": False}
    values, plat = await worklog.calibration(db, off)
    assert values[first.market] == config.market_value(first.market) and plat == {}


async def test_result_without_done_creates_entry(db):
    await seed_world(db)
    config = cfg()
    await Scorer(config, db).run(AS_OF)
    plan = await planmod.build_plan(db, config, AS_OF)
    r = await worklog.record_result(db, plan[1].action_id, views=10, revenue=0.5)
    assert r["status"] == "published" and r["views"] == 10
    assert [e["action_id"] for e in await worklog.entries(db)] == [plan[1].action_id]
