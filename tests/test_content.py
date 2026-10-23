"""Your content: URL parsing, verdicts vs your own normal, daily job, double-down."""

import re
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx

from ode import content
from ode.collectors.youtube import VIDEOS
from ode.config import Secrets
from ode.pipeline import http_client
from ode.scoring import plan as planmod
from ode.scoring.scorer import Scorer
from tests.test_plan_briefs import cfg
from tests.test_scoring_db import AS_OF, seed_world

D0 = date(2026, 1, 1)


def test_parse_url():
    assert content.parse_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3") == \
        ("YOUTUBE", "dQw4w9WgXcQ")
    assert content.parse_url("https://youtu.be/dQw4w9WgXcQ") == ("YOUTUBE", "dQw4w9WgXcQ")
    assert content.parse_url("https://youtube.com/shorts/dQw4w9WgXcQ") == ("SHORTS", "dQw4w9WgXcQ")
    assert content.parse_url("https://m.youtube.com/live/dQw4w9WgXcQ")[1] == "dQw4w9WgXcQ"
    assert content.parse_url("https://play.google.com/store/apps/details?id=com.x.y&hl=en") == \
        ("APP", "com.x.y")
    assert content.parse_url("https://mysite.com/heat-pump-cost/") == \
        ("WEB", "https://mysite.com/heat-pump-cost/")
    for bad in ("not a url", "https://youtube.com/watch?v=short", "https://play.google.com/x"):
        with pytest.raises(content.ContentError):
            content.parse_url(bad)


def hist(points):
    return [(D0 + timedelta(days=a), v) for a, v in points]


def test_value_and_expected_at_age():
    h = hist([(2, 100), (6, 500)])
    assert content.value_at_age(h, D0, 4) == 300                   # interpolated
    assert content.value_at_age(h, D0, 1) == 50                     # from zero at publish
    assert content.value_at_age(h, D0, 9) is None                   # not that old yet
    others = [(hist([(10, v)]), D0) for v in (100, 200, 300)]
    assert content.expected_at_age("YOUTUBE", 10, others) == (200.0, 3)   # your median
    exp, n = content.expected_at_age("YOUTUBE", 28, others[:2])
    assert n == 0 and exp == pytest.approx(600)                     # default 300 x sqrt(4)


def judge(points, age, others=(), platform="YOUTUBE"):
    return content.judge(platform, hist(points), D0, D0 + timedelta(days=age), others)


def test_verdicts():
    assert judge([], 10).kind == "UNKNOWN"
    assert judge([(1, 50)], 1).kind == "TOO_EARLY"
    assert judge([(7, 2000)], 7).kind == "WINNER"                    # 2000 vs 300 default
    assert judge([(7, 400)], 7).kind == "PROMISING"
    assert judge([(7, 100)], 7).kind == "FLAT"
    stalled = [(5, 80), (20, 99), (30, 100)]
    assert judge(stalled, 30).kind == "DROP"                        # old, tiny, stopped
    growing_old = [(10, 100), (50, 300), (60, 400)]
    assert judge(growing_old, 60).kind == "EVERGREEN"
    # your own normal matters: 400 is a WINNER for a channel that usually gets 100
    mine = [(hist([(7, 100)]), D0)] * 3
    assert judge([(7, 400)], 7, mine).kind == "WINNER"
    assert "tumhara normal" in judge([(7, 400)], 7, mine).text
    assert judge([(14, 10)], 14, platform="APP").kind == "FLAT"


def test_position_verdicts():
    assert content.judge_position(3, 5).kind == "TOO_EARLY"
    assert content.judge_position(4, 30).kind == "WINNER"
    assert content.judge_position(15, 30).kind == "PROMISING"
    assert content.judge_position(None, 90).kind == "DROP"
    assert content.judge_position(35, 40).kind == "FLAT"




async def _world(db, monkeypatch):
    await seed_world(db)
    config = cfg()
    await Scorer(config, db).run(AS_OF)
    monkeypatch.setattr(content, "_today", lambda: AS_OF)
    return config


async def test_track_judge_alert_and_double_down(db, monkeypatch):
    config = await _world(db, monkeypatch)
    plan = await planmod.build_plan(db, config, AS_OF)
    yt = next(a for a in plan if a.platform == "YOUTUBE")
    tid = yt.topic_id
    await db.pool.execute("INSERT INTO longtail (topic_id, market, suggestion) VALUES "
                          "($1, $2, 'how does it work'), ($1, $2, 'is it worth it')",
                          tid, yt.market)
    r = await content.add_content(db, "https://youtu.be/AAAAAAAAAAA", action_id=yt.action_id)
    assert r["platform"] == "YOUTUBE" and r["topic_id"] == tid
    assert await db.pool.fetchval("SELECT status FROM work_log WHERE action_id = $1",
                                  yt.action_id) == "published"
    app = await content.add_content(db, "https://play.google.com/store/apps/details?id=com.me")
    with pytest.raises(content.ContentError):
        await content.add_content(db, "https://myblog.com/x")          # platform unknown
    web = await content.add_content(db, "https://myblog.com/x", platform="BLOG",
                                    keyword="heat pump cost", market="en-US")
    assert web["platform"] == "BLOG"

    published = (datetime(AS_OF.year, AS_OF.month, AS_OF.day, tzinfo=UTC)
                 - timedelta(days=8)).isoformat().replace("+00:00", "Z")
    secrets = Secrets(_env_file=None, database_url="x", youtube_api_key="K")  # type: ignore[call-arg]
    with respx.mock() as mock:
        mock.get(VIDEOS).mock(return_value=httpx.Response(200, json={"items": [{
            "id": "AAAAAAAAAAA", "statistics": {"viewCount": "9000", "likeCount": "300"},
            "snippet": {"title": "My hit video", "publishedAt": published}}]}))
        async with http_client(config) as client:
            out = await content.content_job(config, secrets, db, client,
                                            app_fn=lambda pkg, lang, country: {
                                                "realInstalls": 40, "score": 4.1,
                                                "title": "My app"})
            assert re.search(r"id=AAAAAAAAAAA", str(mock.calls[0].request.url))
    assert out["status"] == "ok" and out["youtube"] == 1 and out["apps"] == 1
    assert out["web"] == 0                                     # no DataForSEO credentials
    row = await db.pool.fetchrow("SELECT * FROM my_content WHERE id = $1", r["id"])
    assert row["verdict"] == "WINNER" and row["title"] == "My hit video"
    assert await db.pool.fetchval("SELECT installs FROM content_stats WHERE content_id = $1",
                                  app["id"]) == 40
    alert = await db.pool.fetchrow("SELECT * FROM alerts_sent WHERE kind = 'content'")
    assert alert is not None and "how does it work" in alert["payload"]

    plan2 = await planmod.build_plan(db, config, AS_OF)
    top = plan2[0]
    assert top.variant.startswith(f"more:{r['id']}:") and top.platform == "YOUTUBE"
    assert any("how does it work" in s for s in top.steps)
    assert top.dollars is not None and top.dollars.high > 0
    assert top.action_id != yt.action_id
    # once you take the double-down item it leaves the plan for this week
    from ode import worklog
    await worklog.mark(db, top.action_id)
    assert (await planmod.build_plan(db, config, AS_OF))[0].action_id != top.action_id
    assert await content.untrack(db, r["id"]) and not await content.untrack(db, 99999)


async def test_job_without_content(db):
    async with http_client(cfg()) as client:
        r = await content.content_job(cfg(), Secrets(_env_file=None, database_url="x"),  # type: ignore[call-arg]
                                      db, client)
    assert r["status"] == "skipped"
