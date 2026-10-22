"""Discovery -> watchlist -> Wikipedia collection, end to end.

Upstream APIs are mocked (respx) with realistic payloads; the database is real.
"""

from datetime import UTC, datetime, timedelta

import httpx
import respx

from ode.collectors.wikipedia import WikipediaCollector
from ode.db import DbState
from ode.discovery import Discoverer
from ode.pipeline import http_client, run_one
from ode.quota import InMemoryQuotaStore, QuotaTracker

# --- a tiny fake Wikipedia/Wikidata universe ------------------------------
PAGES = {  # (host, title) -> (canonical title, ns, qid)
    ("en", "Raspberry Pi"): ("Raspberry Pi", 0, "Q100"),
    ("en", "Arduino"): ("Arduino", 0, "Q200"),
    ("en", "Taylor Swift"): ("Taylor Swift", 0, "Q300"),
    ("en", "BeagleBoard"): ("BeagleBoard", 0, "Q400"),
    ("en", "Special:Search"): ("Special:Search", -1, None),
    ("de", "Raspberry Pi"): ("Raspberry Pi", 0, "Q100"),
    ("de", "Bayern Munich FC"): ("Bayern Munich FC", 0, "Q500"),
    ("de", "Wärmepumpe"): ("Wärmepumpe", 0, "Q600"),
    ("de", "Einplatinencomputer"): ("Einplatinencomputer", 0, "Q700"),
}
REDIRECTS = {("en", "Some Redirect"): "Arduino"}
ENTITIES = {
    "Q100": ("Raspberry Pi", ["Q68"], {"enwiki": "Raspberry Pi", "dewiki": "Raspberry Pi"}),
    "Q200": ("Arduino", ["Q68"], {"enwiki": "Arduino", "dewiki": "Arduino"}),
    "Q300": ("Taylor Swift", ["Q5"], {"enwiki": "Taylor Swift", "dewiki": "Taylor Swift"}),
    "Q400": ("BeagleBoard", ["Q68"], {"enwiki": "BeagleBoard"}),
    "Q500": ("FC Bayern", ["Q476028"], {"dewiki": "Bayern Munich FC"}),
    "Q600": ("Heat pump", ["Q11"], {"enwiki": "Heat pump", "dewiki": "Wärmepumpe"}),
    "Q700": ("Single-board computer", ["Q11"],
             {"enwiki": "Single-board computer", "dewiki": "Einplatinencomputer"}),
}
TOP = {
    "en.wikipedia": ["Main_Page", "Special:Search", "Taylor_Swift", "Arduino", "Some_Redirect",
                     "2024_Summer_Olympics"],
    "de.wikipedia": ["Bayern_Munich_FC", "Wärmepumpe"],
}
MORELIKE = {("en", "Raspberry Pi"): ["Arduino", "BeagleBoard"],
            ("de", "Raspberry Pi"): ["Einplatinencomputer"]}


def action_api(request):
    lang = request.url.host.split(".")[0]
    p = request.url.params
    if p.get("list") == "search":
        title = p["srsearch"].removeprefix("morelike:")
        hits = [{"ns": 0, "title": t} for t in MORELIKE.get((lang, title), [])]
        return httpx.Response(200, json={"query": {"search": hits}})
    redirects, pages = [], []
    for t in p["titles"].split("|"):
        target = REDIRECTS.get((lang, t))
        if target:
            redirects.append({"from": t, "to": target})
            t = target
        if (lang, t) in PAGES:
            title, ns, qid = PAGES[(lang, t)]
            page = {"ns": ns, "title": title}
            if qid:
                page["pageprops"] = {"wikibase_item": qid}
            pages.append(page)
        else:
            pages.append({"ns": 0, "title": t, "missing": True})
    return httpx.Response(200, json={"query": {"redirects": redirects, "pages": pages}})


def wikidata(request):
    ents = {}
    for qid in request.url.params["ids"].split("|"):
        label, p31, links = ENTITIES[qid]
        ents[qid] = {
            "id": qid, "labels": {"en": {"value": label}},
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": c}}}} for c in p31]},
            "sitelinks": {s: {"title": t} for s, t in links.items()},
        }
    return httpx.Response(200, json={"entities": ents})


def top(request):
    project = request.url.path.split("/")[6]
    arts = [{"article": a, "views": 1000 - i, "rank": i + 1} for i, a in enumerate(TOP[project])]
    return httpx.Response(200, json={"items": [{"articles": arts}]})


def pageviews(request):
    end = datetime.now(UTC).date() - timedelta(days=1)
    items = [{"timestamp": f"{end - timedelta(days=i):%Y%m%d}00", "views": 10 + i}
             for i in range(3)]
    return httpx.Response(200, json={"items": items})


def mock_universe():
    respx.get(url__regex=r"https://(en|de)\.wikipedia\.org/w/api\.php.*").mock(
        side_effect=action_api)
    respx.get(url__regex=r"https://www\.wikidata\.org/w/api\.php.*").mock(side_effect=wikidata)
    respx.get(url__regex=r"https://wikimedia\.org/.*/pageviews/top/.*").mock(side_effect=top)
    return respx.get(url__regex=r"https://wikimedia\.org/.*/per-article/.*").mock(
        side_effect=pageviews)


async def watch(db, market):
    rows = await db.watchlist_rows(market, "wiki_article")
    return {t: (r["origin"], r["active"], r["excluded_reason"]) for t, r in rows.items()}


@respx.mock
async def test_discovery_then_collection(db, config, secrets):
    pv_route = mock_universe()
    async with http_client(config) as client:
        reports = await Discoverer(config, client, db).run()
    assert {r["status"] for r in reports} == {"ok"}

    en = await watch(db, "en-US")
    assert en["Raspberry Pi"][:2] == ("seed", True)
    assert en["Arduino"][:2] == ("morelike", True)         # niche neighbour label wins
    assert en["BeagleBoard"][:2] == ("morelike", True)
    assert en["Taylor Swift"][1] is False and "human" in en["Taylor Swift"][2]
    assert en["Special:Search"] == ("wiki_top", False, "not an article")
    assert "Main Page" not in en and "2024 Summer Olympics" not in en  # filtered pre-API
    assert "Some Redirect" not in en                                   # resolved to Arduino
    assert en["Heat pump"][:2] == ("langlink", True)                   # from de discovery
    assert en["Single-board computer"][:2] == ("langlink", True)

    de = await watch(db, "de-DE")
    assert de["Raspberry Pi"][:2] == ("seed", True)
    assert de["Wärmepumpe"][:2] == ("wiki_top", True)
    assert de["Einplatinencomputer"][:2] == ("morelike", True)
    assert de["Bayern Munich FC"][1] is False                          # football club excluded
    assert de["Arduino"][:2] == ("langlink", True)                     # cross-language
    assert "BeagleBoard" not in de                                     # no German article

    # cross-language identity: Heat pump (en) and Wärmepumpe (de) are ONE topic
    keys = await db.pool.fetch("SELECT term, topic_key FROM watchlist "
                               "WHERE term IN ('Heat pump', 'Wärmepumpe')")
    assert len({r["topic_key"] for r in keys}) == 1

    # ---- collection over the discovered watchlist
    quota = QuotaTracker(InMemoryQuotaStore(), config.quotas)
    async with http_client(config) as client:
        wiki = WikipediaCollector(config, secrets, client, quota, DbState(db))
        r_en = await run_one(db, wiki, config.market("en-US"), config)
        r_de = await run_one(db, wiki, config.market("de-DE"), config)
    assert r_en["status"] == r_de["status"] == "ok"
    first_calls = pv_route.call_count
    assert first_calls == 5 + 4  # active en (5) + active de (4) articles

    shared = await db.pool.fetchval(
        "SELECT count(DISTINCT market) FROM topic_metrics tm JOIN topics t ON t.id = tm.topic_id "
        "WHERE t.topic_key = 'seed:raspberry_pi'")
    assert shared == 2  # one topic, two markets

    # ---- second run is incremental: only the overlap window is requested
    async with http_client(config) as client:
        wiki = WikipediaCollector(config, secrets, client, quota, DbState(db))
        await run_one(db, wiki, config.market("en-US"), config)
    last_url = str(pv_route.calls[-1].request.url)
    expected_start = datetime.now(UTC).date() - timedelta(days=1 + 3)
    assert f"/daily/{expected_start:%Y%m%d}/" in last_url

    # ---- re-running discovery is idempotent (no new rows, nothing duplicated)
    before = await db.pool.fetchval("SELECT count(*) FROM watchlist")
    async with http_client(config) as client:
        again = await Discoverer(config, client, db).run()
    assert await db.pool.fetchval("SELECT count(*) FROM watchlist") == before
    assert all(r["new"] == 0 for r in again)
    runs = await db.pool.fetch("SELECT collector, status FROM collector_runs")
    assert {r["status"] for r in runs} == {"ok"}


@respx.mock
async def test_blocked_host_fails_fast_not_for_hours(db, config, secrets):
    respx.get(url__regex=r"https://wikimedia\.org/.*").mock(
        side_effect=httpx.ProxyError("403 Forbidden"))
    for i in range(50):
        await db.watchlist_upsert("en-US", "wiki_article", f"T{i}", f"wd:Q{i}", "wiki_top")
    quota = QuotaTracker(InMemoryQuotaStore(), config.quotas)
    async with http_client(config) as client:
        wiki = WikipediaCollector(config, secrets, client, quota, DbState(db))
        result = await run_one(db, wiki, config.market("en-US"), config)
        requests = wiki.http.requests
    assert result["status"] == "error" and "consecutive failures" in result["error"]
    assert requests <= config.circuit_breaker_failures + config.concurrency("wikipedia")


@respx.mock
async def test_one_failing_source_degrades_discovery_to_partial(db, config, secrets):
    respx.get(url__regex=r"https://wikimedia\.org/.*/top/de\.wikipedia/.*").mock(
        return_value=httpx.Response(503))  # German top list down; morelike still works
    mock_universe()  # registered after: the first matching route wins
    async with http_client(config) as client:
        reports = {r["market"]: r for r in await Discoverer(config, client, db).run()}
    assert reports["en-US"]["status"] == "ok"
    assert reports["de-DE"]["status"] == "partial" and reports["de-DE"]["errors"]
    assert "Einplatinencomputer" in await watch(db, "de-DE")  # morelike result kept


async def test_empty_watchlist_is_skipped_not_silently_ok(db, secrets):
    from tests.conftest import make_config

    cfg = make_config(markets=["de-DE"])
    quota = QuotaTracker(InMemoryQuotaStore(), cfg.quotas)
    async with http_client(cfg) as client:
        wiki = WikipediaCollector(cfg, secrets, client, quota, DbState(db))
        result = await run_one(db, wiki, cfg.market("de-DE"), cfg)
    assert result["status"] == "skipped" and "watchlist empty" in result["reason"]
