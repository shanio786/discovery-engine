"""Social trends: TikTok page parsing (tolerant), rising detection, job + plan boost."""

import json

import httpx
import respx

from ode.collectors.youtube import VIDEOS
from ode.config import Secrets
from ode.pipeline import http_client
from ode.realtime import social
from ode.scoring import plan as planmod
from ode.scoring.actions import plan_topic
from ode.scoring.scorer import Scorer
from tests.test_actions import view
from tests.test_plan_briefs import cfg
from tests.test_scoring_db import AS_OF, seed_world


def page(data):
    return (f'<html><head></head><body><script id="__NEXT_DATA__" type="application/json">'
            f"{json.dumps(data)}</script></body></html>")


TAGS = {"props": {"pageProps": {"data": {"list": [
    {"hashtagName": "heatpump", "rank": 3, "publishCnt": 12000, "rankDiff": 40},
    {"hashtag_name": "#FYP", "rank": 1, "publish_cnt": 9e9},
    {"hashtagName": "budgeting", "rank": 2, "videoViews": 5e6, "rankDiffType": 4},
    {"nested": {"hashtagName": "heatpump", "rank": 7}}]}}}}
SOUNDS = {"props": {"x": [{"title": "Song A", "author": "Artist", "clipId": "1", "rank": 1},
                          {"title": "not a sound", "author": "x"}]}}


def test_parsers_are_tolerant():
    data = social.next_data(page(TAGS))
    tags = social.parse_hashtags(data)
    assert [t["name"] for t in tags] == ["fyp", "budgeting", "heatpump"]
    heat = next(t for t in tags if t["name"] == "heatpump")
    assert heat["rank"] == 3 and heat["posts"] == 12000 and heat["rank_change"] == 40
    assert next(t for t in tags if t["name"] == "budgeting")["is_new"]
    assert social.parse_sounds(SOUNDS) == [{"name": "Song A — Artist", "rank": 1,
                                            "rank_change": None, "link": None}]
    assert social.next_data("<html>nothing</html>") is None
    assert social.parse_hashtags(None) == []


def test_rising():
    tags = [{"name": "heatpump", "rank": 3, "rank_change": 40},
            {"name": "fyp", "rank": 1, "rank_change": 99},
            {"name": "calm", "rank": 50, "rank_change": 2},
            {"name": "newone", "rank": 10, "rank_change": None}]
    first = social.rising(tags, {}, rise_min=20, top_new=20)
    assert [t["name"] for t in first] == ["heatpump"]          # no "new" storm on first run
    later = social.rising(tags, {"heatpump": 30, "calm": 51}, rise_min=20, top_new=20)
    assert {t["name"] for t in later} == {"heatpump", "newone"}
    assert next(t for t in later if t["name"] == "heatpump")["jump"] == 27


def test_plan_boost():
    calm = plan_topic(view(phase="STABLE"))
    hot = plan_topic(view(phase="STABLE", social=["TikTok par #heatpump trending (rank 3)"]))
    assert not any(a.platform == "SHORTS" for a in calm)        # stable topic: no shorts ...
    shorts = next(a for a in hot if a.platform == "SHORTS")     # ... unless it trends now
    assert any("TikTok par #heatpump" in w for w in shorts.why)


async def test_social_job(db, monkeypatch):
    await seed_world(db)
    config = cfg()
    config.raw()["markets"] = ["en-US"]
    config.raw()["rate_limits"]["tiktok"] = {"rate": 10_000.0, "burst": 10_000}
    await Scorer(config, db).run(AS_OF)

    async def fake_match(fetcher, market, query):
        return "Heat pump" if query == "heatpump" else None

    async def fake_admit(self, market, titles, origin, **kw):
        return [{"title": t, "canonical": t, "topic_key": "wd:Q1", "active": True}
                for t in titles]
    monkeypatch.setattr("ode.realtime.trends.wiki_match", fake_match)
    monkeypatch.setattr("ode.discovery.Discoverer.admit_titles", fake_admit)
    secrets = Secrets(_env_file=None, database_url="x", youtube_api_key="K")  # type: ignore[call-arg]
    tag_url = social.TIKTOK_URLS["hashtag"].format(cc="US")
    snd_url = social.TIKTOK_URLS["sound"].format(cc="US")
    with respx.mock() as mock:
        mock.get(tag_url).mock(return_value=httpx.Response(200, text=page(TAGS)))
        mock.get(snd_url).mock(return_value=httpx.Response(200, text=page(SOUNDS)))
        mock.get(VIDEOS).mock(return_value=httpx.Response(200, json={"items": [
            {"id": "v9", "snippet": {"title": "Why every Heat Pump is failing?",
                                     "channelTitle": "c"}, "statistics": {"viewCount": "5"}}]}))
        async with http_client(config) as client:
            r = await social.social_job(config, secrets, db, client)
            ua = mock.calls[0].request.headers["user-agent"]
    assert r["status"] == "ok" and r["tiktok_tags"] == 3 and r["tiktok_sounds"] == 1
    assert r["youtube_videos"] == 1 and r["matched"] == 1
    assert r["alerts"] == 2                          # heatpump (jump) + budgeting (new entry)
    assert "Mozilla" in ua
    tid = await db.pool.fetchval("SELECT topic_id FROM social_trends WHERE name = 'heatpump'")
    assert tid == await db.pool.fetchval("SELECT id FROM topics WHERE topic_key = 'wd:Q1'")
    plan = await planmod.build_plan(db, config, AS_OF)
    shorts = next(a for a in plan if a.platform == "SHORTS" and a.topic == "Heat pump")
    assert any("TikTok par #heatpump" in w for w in shorts.why)
    assert any("YouTube trending mein 1 video" in w for w in shorts.why)


async def test_tiktok_blocked_does_not_break_youtube(db):
    config = cfg()
    config.raw()["markets"] = ["en-US"]
    secrets = Secrets(_env_file=None, database_url="x", youtube_api_key="K")  # type: ignore[call-arg]
    with respx.mock() as mock:
        mock.get(url__startswith="https://ads.tiktok.com/").mock(
            return_value=httpx.Response(403))
        mock.get(VIDEOS).mock(return_value=httpx.Response(200, json={"items": []}))
        async with http_client(config) as client:
            r = await social.social_job(config, secrets, db, client)
    assert r["status"] == "error" and r["errors"] == 1          # nothing at all came in
