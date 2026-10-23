"""Weekly plan: choice under an hours budget, spread, upkeep, schedule, weekly job."""

import re
from datetime import UTC, datetime, time, timedelta

from ode import web
from ode.scheduler import Job, is_due
from ode.scoring import portfolio as pf
from ode.scoring.actions import Action
from ode.scoring.dollars import Dollars
from ode.scoring.scorer import Scorer
from tests.test_plan_briefs import cfg
from tests.test_scoring_db import AS_OF, seed_world
from tests.test_web import BASE, _Day, client, secrets


def act(platform, market, low, high, hours, topic="t", variant="", why=("➖ STABLE",)):
    a = Action(platform, market, market[:2], f"{topic}-{platform}-{market}", 1.0, 50, 50,
               list(why), [], [], None, variant=variant)
    a.dollars = Dollars(low, high, hours, "model")
    return a


def test_choose_respects_budget_caps_and_mix():
    cands = [act("BLOG", "en-US", 100, 400, 10), act("BLOG", "de-DE", 90, 360, 10),
             act("BLOG", "ja-JP", 80, 320, 10), act("BLOG", "fr-FR", 70, 300, 10),
             act("SHORTS", "en-US", 5, 40, 5, why=("🚀 BREAKOUT",)),
             act("APP", "en-US", 50, 200, 40)]
    week = pf.choose(cands, 30, cfg={"max_per_platform": 2})
    kinds = [(i.platform, i.market) for i in week.items]
    assert week.hours <= 30
    assert sum(p == "BLOG" for p, _ in kinds) == 2                # platform cap
    assert ("APP", "en-US") not in kinds                          # 40 h never fits 30
    assert ("SHORTS", "en-US") in kinds                           # fast work mixed in
    lo, hi = week.dollars
    assert lo < hi and lo >= 100


def test_double_down_first_and_upkeep_capped():
    more = act("YOUTUBE", "en-US", 10, 50, 12, variant="more:1:2026w27")
    big = act("BLOG", "en-US", 500, 2000, 10)
    upkeep = [pf.WeekItem("upkeep", f"run{i}", "YOUTUBE", "en-US", 3) for i in range(10)]
    week = pf.choose([big, more], 20, upkeep=upkeep, cfg={"max_upkeep_share": 0.5})
    assert sum(i.hours for i in week.items if i.kind == "upkeep") <= 10
    assert any(i.kind == "more" for i in week.items)               # winners always first
    assert pf.choose([], 10).items == []


def test_schedule_spreads_hours_over_days():
    week = pf.choose([act("BLOG", "en-US", 100, 400, 9), act("SHORTS", "de-DE", 5, 40, 3)], 12)
    sched = pf.schedule(week, days=3)
    assert list(sched) == ["Mon", "Tue", "Wed"]
    assert sum(h for parts in sched.values() for _, h in parts) == 12
    blog = next(i for i in week.items if i.platform == "BLOG")
    assert blog.days == ["Mon", "Tue", "Wed"] or len(blog.days) >= 2   # 9 h spans days
    text = pf.render_week(week, 3)
    assert "Is hafte ka plan — 12 / 12 ghante" in text and "Mon:" in text


def test_weekly_job_due_once_per_week():
    job = Job("week", at=time(6, 57, tzinfo=UTC), weekday=0)
    monday = datetime(2026, 6, 29, 7, 0, tzinfo=UTC)               # a Monday
    assert is_due(job, None, monday)
    assert not is_due(job, monday, monday + timedelta(days=2))    # ran this week
    assert not is_due(job, None, monday - timedelta(days=1))       # Sunday: last slot too old
    assert is_due(job, monday, monday + timedelta(days=7))         # next Monday


async def test_build_week_and_dashboard(db, monkeypatch):
    await seed_world(db)
    config = cfg()
    await Scorer(config, db).run(AS_OF)
    week = await pf.build_week(db, config, AS_OF, hours=25)
    assert week.items and week.hours <= 25 and week.budget == 25
    assert await pf.hours_setting(db, config) == 20                 # config default
    await pf.save_hours(db, 12)
    assert await pf.hours_setting(db, config) == 12
    monkeypatch.setattr(web, "datetime", _Day)
    app = web.create_app(config, secrets(), db)
    async with client(app) as c:
        page = await c.get("/week")
        assert page.status_code == 200 and "/ 12 ghante" in page.text
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        r = await c.post("/hours", data={"hours": "30", "csrf": token}, headers={"Origin": BASE})
        assert r.status_code == 303 and await pf.hours_setting(db, config) == 30
        bad = await c.post("/hours", data={"hours": "500", "csrf": token},
                           headers={"Origin": BASE})
        assert bad.status_code == 400
