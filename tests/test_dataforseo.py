"""DataForSEO verify layer (API mocked with respx): parsing, budget, plan use."""

import json

import httpx
import respx

from ode import dataforseo as dfs
from ode.config import Secrets
from ode.pipeline import http_client
from ode.scoring import plan as planmod
from ode.scoring.actions import google_ease, plan_topic
from ode.scoring.scorer import Scorer
from tests.test_actions import view
from tests.test_plan_briefs import cfg
from tests.test_scoring_db import AS_OF, seed_world


def ok(result, cost=0.05):
    return {"status_code": 20000, "cost": cost,
            "tasks": [{"status_code": 20000, "cost": cost, "result": result}]}


def test_sanitize_and_domains():
    assert dfs.sanitize_keyword("How (much) does a Heat-Pump cost?!") == \
        "how much does a heat-pump cost"
    assert dfs.sanitize_keyword("  ,,, ") is None
    assert len(dfs.sanitize_keyword("word " * 30).split()) == 10
    assert dfs.domain_of("https://www.Example.com/a") == "example.com"
    assert dfs.is_weak("reddit.com") and dfs.is_weak("x.org", "https://x.org/forum/t/1")
    assert not dfs.is_weak("energy.gov", "https://energy.gov/heat-pumps")


def test_parsers_and_errors():
    vols, cost, err = dfs.parse_volumes(ok([{"keyword": "Heat Pump", "search_volume": 9900,
                                             "cpc": 2.1}]))
    assert vols["heat pump"]["volume"] == 9900 and cost == 0.05 and err is None
    kd, _, _ = dfs.parse_difficulty(ok([{"items": [{"keyword": "heat pump",
                                                     "keyword_difficulty": 42}]}]))
    assert kd == {"heat pump": 42.0}
    serp, _, _ = dfs.parse_serp(ok([{"items": [
        {"type": "organic", "rank_group": 2, "domain": "www.reddit.com", "url": "u2"},
        {"type": "paid", "rank_group": 1, "domain": "ads.com"},
        {"type": "organic", "rank_group": 1, "domain": "energy.gov", "url": "u1"}]}]))
    assert [r["domain"] for r in serp] == ["energy.gov", "reddit.com"] and serp[1]["weak"]
    bad = {"status_code": 20000, "cost": 0, "tasks": [{"status_code": 40501,
                                                       "status_message": "Invalid Field"}]}
    assert dfs.parse_volumes(bad)[2] == "40501 Invalid Field"
    assert dfs.parse_volumes(None)[2] == "empty response"


def test_google_ease_replaces_the_guess():
    assert google_ease({}) == (None, [])
    easy, why = google_ease({"kd": 10, "serp_weak_share": 0.5, "gsv": 2400})
    hard, _ = google_ease({"kd": 80, "serp_weak_share": 0.0})
    assert easy > 70 and hard < 30 and any("2,400 searches" in w for w in why)
    blog = next(a for a in plan_topic(view(google={"kd": 10.0, "serp_weak_share": 0.4}))
                if a.platform == "BLOG")
    assert blog.ease > 70
    assert not any("DataForSEO" in u for u in blog.unverified)
    guess = next(a for a in plan_topic(view()) if a.platform == "BLOG")
    assert any("DataForSEO" in u for u in guess.unverified)


def secrets():
    return Secrets(_env_file=None, database_url="x", dataforseo_login="L",  # type: ignore[call-arg]
                   dataforseo_password="P")


def mock_api(mock, volume_cost=0.075):
    def volumes(request):
        kws = json.loads(request.content)[0]["keywords"]
        return httpx.Response(200, json=ok([{"keyword": k, "search_volume": 1000 + i,
                                             "cpc": 1.5} for i, k in enumerate(kws)],
                                           cost=volume_cost))

    def kd(request):
        kws = json.loads(request.content)[0]["keywords"]
        return httpx.Response(200, json=ok([{"items": [{"keyword": k, "keyword_difficulty": 12}
                                                       for k in kws]}], cost=0.02))
    mock.post(dfs.VOLUME).mock(side_effect=volumes)
    mock.post(dfs.DIFFICULTY).mock(side_effect=kd)
    mock.post(dfs.SERP).mock(return_value=httpx.Response(200, json=ok([{"items": [
        {"type": "organic", "rank_group": i + 1, "domain": d, "url": f"https://{d}/x"}
        for i, d in enumerate(["reddit.com", "quora.com", "big.com", "energy.gov"])]}],
        cost=0.002)))


async def _world(db, monkeypatch):
    await seed_world(db)
    config = cfg()
    config.raw()["rate_limits"]["dataforseo"] = {"rate": 10_000.0, "burst": 10_000}
    await Scorer(config, db).run(AS_OF)
    monkeypatch.setattr(dfs, "datetime", _Day)
    return config


class _Day:
    @staticmethod
    def now(tz=None):
        from datetime import datetime
        return datetime(AS_OF.year, AS_OF.month, AS_OF.day, 5, tzinfo=tz)


async def test_verify_end_to_end(db, monkeypatch):
    config = await _world(db, monkeypatch)
    tid = await db.pool.fetchval("SELECT id FROM topics WHERE topic_key = 'wd:Q1'")
    await db.pool.execute("INSERT INTO longtail (topic_id, market, suggestion) VALUES "
                          "($1, 'en-US', 'heat pump cost'), ($1, 'en-US', 'how heat pump works')",
                          tid)
    with respx.mock() as mock:
        mock_api(mock)
        async with http_client(config) as client:
            r = await dfs.run_verify(db, config, secrets(), client)
            calls = len(mock.calls)
            again = await dfs.run_verify(db, config, secrets(), client)   # fresh: nothing due
            assert calls == len(mock.calls)
        vol_req = next(c.request for c in mock.calls if str(c.request.url) == dfs.VOLUME)
    assert r["status"] == "ok" and r["topics"] >= 1 and r["spent_usd"] > 0
    assert again == {"status": "skipped", "reason": "nothing due"}
    body = json.loads(vol_req.content)[0]
    assert body["location_name"] == "United States" and body["language_code"] == "en"
    assert {"heat pump", "heat pump cost"} <= set(body["keywords"])
    assert vol_req.headers["authorization"].startswith("Basic ")
    assert await db.pool.fetchval("SELECT volume FROM longtail WHERE suggestion = "
                                  "'heat pump cost'") is not None
    assert await db.pool.fetchval("SELECT count(*) FROM serp_results WHERE topic_id = $1 "
                                  "AND market = 'en-US'",
                                  tid) == 4
    await Scorer(config, db).run(AS_OF)
    plan = await planmod.build_plan(db, config, AS_OF)
    blog = next(a for a in plan if a.platform == "BLOG" and a.topic_id == tid
                and a.market == "en-US")
    assert any("Google difficulty 12/100" in w for w in blog.why)
    assert any("forum / user pages" in w for w in blog.why)
    assert not any("DataForSEO" in u for u in blog.unverified)


async def test_budget_cap_stops_spending(db, monkeypatch):
    config = await _world(db, monkeypatch)
    config.raw()["dataforseo"] = {"daily_budget_usd": 0.08}     # one volume call only
    with respx.mock(assert_all_called=False) as mock:
        mock_api(mock)
        async with http_client(config) as client:
            r = await dfs.run_verify(db, config, secrets(), client)
        assert sum(str(c.request.url) == dfs.VOLUME for c in mock.calls) == 1
    assert r["budget_stop"] is True
    used = await db.pool.fetchval("SELECT units_used FROM api_quota WHERE api = 'dataforseo'")
    assert used <= 800


async def test_skips_without_credentials(db):
    config = cfg()
    async with http_client(config) as client:
        r = await dfs.run_verify(db, config, Secrets(_env_file=None, database_url="x"),  # type: ignore[call-arg]
                                 client)
    assert r["status"] == "skipped" and "DATAFORSEO_LOGIN" in r["reason"]
