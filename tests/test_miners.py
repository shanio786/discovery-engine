"""Research miners: autocomplete (respx), Play Store complaints (injected
scraper), serial-outlier formats (SQL) + Gemini formula (respx)."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import respx

from ode import miners
from ode.briefs import build_prompt
from ode.config import Secrets
from ode.pipeline import http_client
from ode.scoring import plan as planmod
from ode.scoring.scorer import Scorer
from tests.test_plan_briefs import action, cfg
from tests.test_scoring_db import AS_OF, seed_world


def test_suggest_seeds_per_language():
    en = miners.suggest_seeds("heat pump", "en")
    assert en[0] == "heat pump" and "how heat pump" in en and "heat pump z" in en
    ur = miners.suggest_seeds("سولر", "ur")
    assert "کیسے سولر" in ur and not any(s.endswith(" z") for s in ur)   # no a-z for Urdu


def test_parse_and_rank_suggestions():
    assert miners.parse_suggestions(["q", ["a", " b ", ""]]) == ["a", "b"]
    assert miners.parse_suggestions({"x": 1}) == [] and miners.parse_suggestions(None) == []
    ranked = miners.rank_suggestions({"heat pump": "s", "heat pump cost": "s",
                                      "how heat pump works": "s", "heat pump vs ac": "s"},
                                     "heat pump", 10)
    assert ranked[0][0] == "how heat pump works"       # questions first
    assert "heat pump" not in [s for s, _ in ranked]                 # the query itself dropped


def test_pick_complaints_prefers_useful_reviews():
    revs = [{"reviewId": "1", "content": "bad", "thumbsUpCount": 99},
            {"reviewId": "2", "content": "Crashes every time I add a second account", "thumbsUpCount": 3},
            {"reviewId": "3", "content": "Full of ads, impossible to use the timer feature", "thumbsUpCount": 40},
            {"content": "no id here but long enough to count as a review"}]
    assert [r["reviewId"] for r in miners.pick_complaints(revs)] == ["3", "2"]


async def _world(db):
    await seed_world(db)
    config = cfg()
    fast = {"rate": 10_000.0, "burst": 10_000}
    config.raw()["rate_limits"].update(autocomplete=fast, playstore=fast, gemini=fast)
    await Scorer(config, db).run(AS_OF)
    return config


def _today():
    return AS_OF


async def test_autocomplete_miner_stores_and_attaches(db, monkeypatch):
    monkeypatch.setattr(miners, "_today", _today)
    config = await _world(db)
    config.raw()["miners"] = {"autocomplete": {"max_topics": 2, "letters": False}}

    def answer(request):
        q = request.url.params["q"]
        return httpx.Response(200, json=[q, [q, f"{q} cost", f"{q} for beginners"]])

    with respx.mock() as mock:
        route = mock.get(miners.SUGGEST).mock(side_effect=answer)
        async with http_client(config) as client:
            r = await miners.mine_autocomplete(db, config, client)
            again = await miners.mine_autocomplete(db, config, client)   # fresh: no refetch
    assert r["status"] == "ok" and r["topics"] == 2 and r["suggestions"] > 0
    calls = route.call_count
    assert again["topics"] == 0 and route.call_count == calls
    assert route.calls[0].request.url.params["hl"] in ("en", "de")
    plan = await planmod.build_plan(db, config, AS_OF)
    top = plan[0]
    assert top.questions and any("for beginners" in q for q in top.questions)
    md = planmod.render_plan_markdown(plan, AS_OF)
    assert "Log yeh sawal poochh rahe hain" in md


async def test_complaints_miner_with_injected_scraper(db, monkeypatch):
    monkeypatch.setattr(miners, "_today", _today)
    config = await _world(db)
    calls = []

    def search(q, n_hits, lang, country):
        calls.append((q, lang, country))
        return [{"appId": "com.a"}, {"appId": "com.b"}, {"title": "no id"}]

    def reviews(app_id, lang, country, count):
        return [{"reviewId": f"{app_id}-1", "content": f"{app_id} crashes whenever I sync data",
                 "thumbsUpCount": 5, "at": datetime(2026, 6, 1, tzinfo=UTC).replace(tzinfo=None)}]  # naive, like the scraper

    r = await miners.mine_complaints(db, config, search_fn=search, reviews_fn=reviews)
    assert r["status"] == "ok" and r["topics"] >= 1 and r["complaints"] >= 2
    assert all(c[1] in ("en", "de") for c in calls)
    at = await db.pool.fetchval("SELECT at FROM app_complaints LIMIT 1")
    assert at.tzinfo is not None
    plan = await planmod.build_plan(db, config, AS_OF)
    app = next(a for a in plan if a.platform == "APP")
    assert any("crashes" in c for c in app.complaints)
    assert not next(a for a in plan if a.platform == "BLOG").complaints   # only APP/TOOLSITE

    def broken(*a, **k):
        raise RuntimeError("Play markup changed")
    await db.pool.execute("DELETE FROM app_complaints")
    bad = await miners.mine_complaints(db, config, search_fn=broken, reviews_fn=reviews)
    assert bad["status"] == "error" and bad["errors"] >= 1


async def test_formats_miner_finds_serial_outliers_and_gemini_formula(db, monkeypatch):
    monkeypatch.setattr(miners, "_today", _today)
    config = await _world(db)
    tid = await db.pool.fetchval("SELECT id FROM topics WHERE topic_key = 'wd:Q1'")
    await db.pool.executemany(
        "INSERT INTO channels (id, title, subs) VALUES ($1, $2, $3)",
        [("small", "Tiny Heat", 1000), ("big", "Mega", 5_000_000), ("meh", "Meh", 1000)])
    now = datetime.now(UTC)
    vids = [("v1", "small", "Heat pump in 60 seconds", 90_000), ("v2", "small",
            "Heat pump mistakes", 20_000), ("v3", "big", "Heat pump explained", 9_000_000),
            ("v4", "meh", "meh video", 2_000), ("v5", "meh", "meh hit", 50_000)]
    await db.pool.executemany(
        "INSERT INTO videos (id, channel_id, topic_id, title, views, published_at, market) "
        "VALUES ($1, $2, $3, $4, $5, $6, 'en-US')",
        [(v, ch, tid, t, n, now - timedelta(days=10)) for v, ch, t, n in vids])
    secrets = Secrets(_env_file=None, database_url="x", gemini_api_key="G")  # type: ignore[call-arg]
    reply = {"candidates": [{"content": {"parts": [{"text": "[X] in 60 seconds: hook, 3 facts"}]}}]}
    with respx.mock() as mock:
        mock.post(url__regex=r"https://generativelanguage\.googleapis\.com/.*").mock(
            return_value=httpx.Response(200, json=reply))
        async with http_client(config) as client:
            r = await miners.mine_formats(db, config, secrets, client)
    assert r == {"status": "ok", "channels": 1, "formulas": 1}   # big = celebrity, meh = 1 hit
    row = await db.pool.fetchrow("SELECT * FROM formats")
    assert row["channel_id"] == "small" and row["outlier_count"] == 2
    assert json.loads(row["titles"])[0] == "Heat pump in 60 seconds"
    assert row["formula"].startswith("[X] in 60 seconds")
    assert await db.pool.fetchval("SELECT is_serial_outlier FROM channels WHERE id = 'small'")
    plan = await planmod.build_plan(db, config, AS_OF)
    yt = next(a for a in plan if a.platform == "YOUTUBE" and a.topic_id == tid)
    assert yt.formats and yt.formats[0]["channel"] == "Tiny Heat"
    assert "Saabit viral format" in planmod.render_plan_markdown(plan, AS_OF)
    assert "[X] in 60 seconds" in build_prompt(yt)


async def test_run_miners_isolates_failures(db, monkeypatch):
    monkeypatch.setattr(miners, "_today", _today)
    config = await _world(db)
    config.raw()["miners"] = {"autocomplete": {"enabled": False}, "complaints": {"enabled": False}}

    async def boom(*a, **k):
        raise RuntimeError("formats broke")
    monkeypatch.setattr(miners, "mine_formats", boom)
    async with http_client(config) as client:
        out = await miners.run_miners(db, config, Secrets(_env_file=None, database_url="x"),  # type: ignore[call-arg]
                                      client)
    assert out["autocomplete"]["status"] == "skipped" and out["formats"]["status"] == "error"
    assert await db.pool.fetchval(
        "SELECT status FROM collector_runs WHERE collector = 'miner_formats'") == "error"


def test_prompt_includes_research():
    a = action(questions=["wie teuer ist eine wärmepumpe"], complaints=["App stürzt ab"])
    p = build_prompt(a)
    assert "wie teuer ist eine wärmepumpe" in p and "App stürzt ab" in p
