"""Realtime jobs end to end on a real DB with mocked upstreams."""

import gzip
import json
from datetime import UTC, datetime, timedelta

import httpx
import respx

from ode.config import Secrets, load_config
from ode.notify import Notifier
from ode.pipeline import http_client
from ode.realtime import runner
from ode.scoring.scorer import Scorer
from tests import test_discovery_e2e as uni
from tests.conftest import make_config
from tests.test_scoring_db import AS_OF, seed_world

TOKEN = "123:SECRET"


def cfg(**extra):
    real = load_config()
    return make_config(
        realtime={"hourly": {"enabled": True, "min_views_per_hour": 10, "window_hours": 3,
                             "baseline_hours": 48, "spike_ratio": 5.0, "min_window_views": 600,
                             "new_article_views": 100_000, "max_alerts_per_run": 10},
                  "trends": {"enabled": True, "min_traffic": 2000, "add_to_watchlist": True,
                             "max_alerts_per_run": 5}},
        alerts=real.alerts, monetization=real.monetization, scoring=real.scoring,
        actions={"top_n": 10, "per_market_max": 10}, **extra)


def secrets(token=TOKEN):
    return Secrets(_env_file=None, database_url="x", telegram_bot_token=token,  # type: ignore[call-arg]
                   telegram_chat_id="42" if token else None)


def telegram_route():
    return respx.post(url__regex=r"https://api\.telegram\.org/bot.*/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True}))


def dump(lines: list[str]) -> bytes:
    return gzip.compress(("\n".join(lines) + "\n").encode())


@respx.mock
async def test_hourly_job_spike_to_alert_once(db):
    uni.mock_universe()
    tg = telegram_route()
    respx.get(url__regex=r"https://dumps\.wikimedia\.org/.*").mock(return_value=httpx.Response(
        200, content=dump(["en Arduino 250 0", "en.m Arduino 150 0", "en Taylor_Swift 900 0",
                           "en Main_Page 99999 0", "de Arduino 1 0"])))
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    # quiet baseline for the 2 days before the dumps we will ingest
    rows = [("en.wikipedia", t, now - timedelta(hours=h), v)
            for h in range(7, 55) for t, v in (("Arduino", 40), ("Taylor Swift", 90))]
    await db.pool.executemany("INSERT INTO hourly_views VALUES ($1,$2,$3,$4)", rows)
    config = cfg()
    async with http_client(config) as client:
        first = await runner.hourly_job(config, secrets(), db, client)
        second = await runner.hourly_job(config, secrets(), db, client)
    assert first["hours_ingested"] == 6 and first["alerts"] == 1
    assert second["hours_ingested"] == 0 and second["alerts"] == 0      # idempotent + dedup
    assert tg.call_count == 1
    body = json.loads(tg.calls[0].request.content)
    assert "Arduino" in body["text"] and "Taylor" not in body["text"]   # people excluded
    assert TOKEN not in str(tg.calls[0].request.content)
    w = await db.watchlist_rows("en-US", "wiki_article")
    assert w["Arduino"]["origin"] == "realtime" and w["Arduino"]["active"]
    assert await db.pool.fetchval("SELECT count(*) FROM hourly_ingested") == 6


@respx.mock
async def test_trends_job(db):
    tg = telegram_route()
    us = """<rss xmlns:ht="https://trends.google.com/trending/rss"><channel>
      <item><title>arduino uno</title><ht:approx_traffic>50,000+</ht:approx_traffic></item>
      <item><title>taylor swift tour</title><ht:approx_traffic>200K+</ht:approx_traffic></item>
      <item><title>mystery thing</title><ht:approx_traffic>5,000+</ht:approx_traffic></item>
      <item><title>too small</title><ht:approx_traffic>100+</ht:approx_traffic></item>
    </channel></rss>"""
    respx.get(url__regex=r"https://trends\.google\.com/.*geo=US").mock(
        return_value=httpx.Response(200, text=us))
    respx.get(url__regex=r"https://trends\.google\.com/.*geo=DE").mock(
        return_value=httpx.Response(200, text="<rss><channel></channel></rss>"))
    search = {"arduino uno": "Arduino", "taylor swift tour": "Taylor Swift"}

    def api(request):
        p = request.url.params
        if p.get("list") == "search" and not p["srsearch"].startswith("morelike:"):
            hit = search.get(p["srsearch"])
            return httpx.Response(200, json={"query": {"search": [{"ns": 0, "title": hit}]
                                                       if hit else []}})
        return uni.action_api(request)

    respx.get(url__regex=r"https://(en|de)\.wikipedia\.org/w/api\.php.*").mock(side_effect=api)
    respx.get(url__regex=r"https://www\.wikidata\.org/w/api\.php.*").mock(side_effect=uni.wikidata)
    config = cfg()
    async with http_client(config) as client:
        out = await runner.trends_job(config, secrets(), db, client)
    assert out["status"] == "ok" and out["items"] == 4 and out["matched"] == 1
    texts = [json.loads(c.request.content)["text"] for c in tg.calls]
    assert len(texts) == 2                                   # arduino + mystery; not taylor
    assert any("arduino uno" in t for t in texts) and any("mystery thing" in t for t in texts)
    assert not any("taylor" in t.lower() for t in texts)
    row = await db.pool.fetchrow("SELECT * FROM trend_items WHERE title = 'arduino uno'")
    assert row["wiki_title"] == "Arduino" and row["traffic"] == 50_000
    assert (await db.watchlist_rows("en-US", "wiki_article"))["Arduino"]["origin"] == "trend"


@respx.mock
async def test_breakout_and_plan_messages(db):
    tg = telegram_route()
    await seed_world(db)
    config = cfg()
    await Scorer(config, db).run(AS_OF)
    await db.pool.execute("UPDATE scores SET spike_ratio = 4.0 WHERE phase = 'BREAKOUT'")
    async with http_client(config) as client:
        b1 = await runner.breakout_job(config, secrets(), db, client)
        b2 = await runner.breakout_job(config, secrets(), db, client)
        from unittest.mock import patch
        with patch("ode.realtime.runner.datetime") as fake:
            fake.now.return_value = datetime.combine(AS_OF, datetime.min.time(), tzinfo=UTC)
            p1 = await runner.plan_message_job(config, secrets(), db, client)
            p2 = await runner.plan_message_job(config, secrets(), db, client)
    assert b1["alerts"] == 1 and b2["alerts"] == 0
    assert p1["status"] == "ok" and p2["status"] == "skipped"
    texts = [json.loads(c.request.content)["text"] for c in tg.calls]
    assert "BREAKOUT" in texts[0] and "Heat pump" in texts[0]
    assert "Aaj kya karna hai" in texts[1] and "Heat pump" in texts[1]


@respx.mock
async def test_without_telegram_alerts_are_still_logged(db):
    tg = telegram_route()
    config = cfg()
    async with http_client(config) as client:
        n = Notifier(config, secrets(token=None), client, db)
        assert not n.enabled
        assert await n.send_once("k1", "test", "hello", cooldown=timedelta(hours=1))
        assert not await n.send_once("k1", "test", "hello", cooldown=timedelta(hours=1))
    assert tg.call_count == 0
    assert await db.pool.fetchval("SELECT payload->>'text' FROM alerts_sent") == "hello"


async def test_reports_job_writes_dated_and_latest_files(db, tmp_path, monkeypatch):
    from unittest.mock import patch

    await seed_world(db)
    config = cfg()
    await Scorer(config, db).run(AS_OF)
    monkeypatch.setenv("ODE_REPORTS_DIR", str(tmp_path))
    with patch("ode.realtime.runner.datetime") as fake:
        fake.now.return_value = datetime.combine(AS_OF, datetime.min.time(), tzinfo=UTC)
        fake.strptime = datetime.strptime
        async with http_client(config) as client:
            out = await runner.reports_job(config, secrets(), db, client)
    names = {p.name for p in tmp_path.iterdir()}
    assert {f"plan-{AS_OF}.md", "plan-latest.md", "gems-latest.md", "gems-latest.csv"} <= names
    assert "Aaj kya karna hai" in (tmp_path / "plan-latest.md").read_text()
    assert out["status"] == "ok"
