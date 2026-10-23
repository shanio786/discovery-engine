"""Competitor watch: pure detectors, daily job (YouTube mocked), plan warnings."""

from datetime import date

import httpx
import respx

from ode import competitors as comp
from ode import worklog
from ode.collectors.youtube import CHANNELS, SEARCH, VIDEOS
from ode.config import Secrets
from ode.models import RawSignal
from ode.pipeline import http_client
from ode.scoring import plan as planmod
from ode.scoring.actions import plan_topic
from ode.scoring.scorer import Scorer
from tests.test_actions import view
from tests.test_plan_briefs import cfg
from tests.test_scoring_db import AS_OF, seed_world


def test_detectors():
    vids = [{"id": "a", "channel_id": "big", "views": 10}, {"id": "b", "channel_id": "big",
                                                             "views": 99},
            {"id": "c", "channel_id": "small", "views": 5000}]
    chans = {"big": {"title": "Mega", "subs": 2_000_000}, "small": {"title": "s", "subs": 900}}
    ev = comp.big_channel_events(vids, chans, 250_000)
    assert [(e["channel"], e["id"]) for e in ev] == [("Mega", "b")]     # best video, once
    assert comp.is_surge(60, [43.5, 43.5], ratio=3, min_new=10) == (True, 6.0)
    assert comp.is_surge(12, [43.5], ratio=3, min_new=10) == (False, 1.2)
    assert comp.is_surge(None, [10], ratio=3, min_new=10) == (False, None)
    assert comp.is_surge(40, [1.0], ratio=3, min_new=10)[0]             # from ~nothing
    assert comp.serp_newcomers(["new.com", "a.com", "b.com"], ["a.com", "b.com"]) == ["new.com"]


def test_plan_warns_and_penalises():
    calm = {a.platform: a for a in plan_topic(view())}
    hot = {a.platform: a for a in plan_topic(view(competitors=[
        {"kind": "big_channel", "channel": "Mega", "subs": 2_000_000},
        {"kind": "serp_newcomer", "domain": "new.com"}]))}
    assert hot["YOUTUBE"].ease < calm["YOUTUBE"].ease
    assert any("bara channel aa gaya: Mega" in w for w in hot["YOUTUBE"].why)
    assert any("naya site: new.com" in w for w in hot["BLOG"].why)


def yt_mock(mock):
    mock.get(SEARCH).mock(return_value=httpx.Response(200, json={
        "pageInfo": {"totalResults": 80}, "items": [{"id": {"videoId": "v1"}},
                                                    {"id": {"videoId": "v2"}}]}))
    mock.get(VIDEOS).mock(return_value=httpx.Response(200, json={"items": [
        {"id": "v1", "snippet": {"title": "Heat pumps explained", "channelId": "big"},
         "statistics": {"viewCount": "90000"}},
        {"id": "v2", "snippet": {"title": "my take", "channelId": "small"},
         "statistics": {"viewCount": "100"}}]}))
    mock.get(CHANNELS).mock(return_value=httpx.Response(200, json={"items": [
        {"id": "big", "snippet": {"title": "MegaScience"}, "statistics":
         {"subscriberCount": "3000000"}},
        {"id": "small", "snippet": {"title": "tiny"}, "statistics": {"subscriberCount": "50"}}]}))


async def test_watch_job_events_once_and_plan(db, monkeypatch):
    monkeypatch.setattr(comp, "_today", lambda: AS_OF)
    await seed_world(db)
    config = cfg()
    await Scorer(config, db).run(AS_OF)
    plan = await planmod.build_plan(db, config, AS_OF)
    mine = next(a for a in plan if a.topic == "Heat pump" and a.market == "en-US")
    await worklog.mark(db, mine.action_id)
    tid = mine.topic_id
    await db.upsert_signals([RawSignal("youtube", "en-US", "Heat pump",
                                       "youtube_monthly_uploads", 40.0, date(2026, m, 1),
                                       topic_key="wd:Q1") for m in (3, 4, 5)])
    for i, doms in enumerate([["a.com", "b.com", "c.com"], ["new.com", "a.com", "b.com"]]):
        await db.pool.executemany(
            "INSERT INTO serp_results (topic_id, market, position, domain, fetched_at) "
            "VALUES ($1, 'en-US', $2, $3, now() - make_interval(days => $4))",
            [(tid, p + 1, d, 10 - i * 5) for p, d in enumerate(doms)])
    secrets = Secrets(_env_file=None, database_url="x", youtube_api_key="K")  # type: ignore[call-arg]
    config.raw()["competitors"] = {"max_topics": 1, "top_plan": 0}
    with respx.mock() as mock:
        yt_mock(mock)
        async with http_client(config) as client:
            r1 = await comp.watch_job(config, secrets, db, client)
            r2 = await comp.watch_job(config, secrets, db, client)
    assert r1["status"] == "ok" and r1["topics"] == 1
    kinds = {r["kind"] for r in await db.pool.fetch("SELECT kind FROM competitor_events")}
    assert kinds == {"big_channel", "upload_surge", "serp_newcomer"}
    assert r1["events"] == 3 and r2["events"] == 0                     # stored once
    assert await db.pool.fetchval("SELECT count(*) FROM alerts_sent WHERE kind = "
                                  "'competitor'") == 3
    assert await db.pool.fetchval("SELECT value FROM topic_metrics WHERE metric_name = "
                                  "'youtube_weekly_uploads'") == 80
    plan2 = await planmod.build_plan(db, config, AS_OF)
    yt = next(a for a in plan2 if a.platform == "YOUTUBE" and a.topic == "Heat pump"
              and a.market == "en-US")
    assert any("MegaScience" in w for w in yt.why)
