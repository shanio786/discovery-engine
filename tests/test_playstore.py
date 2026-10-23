from datetime import UTC, date, datetime, timedelta

import httpx

from ode.collectors.playstore import PlayStoreCollector, parse_installs, playstore_metrics
from ode.models import Probe
from ode.quota import InMemoryQuotaStore, QuotaTracker
from ode.state import InMemoryState
from tests.conftest import make_config

TODAY = date(2026, 6, 30)


def ts(days_ago):
    return int(datetime(2026, 6, 30, tzinfo=UTC).timestamp()) - days_ago * 86_400


def test_parse_installs():
    assert parse_installs("1,000,000+") == 1_000_000
    assert parse_installs("10.000+") == 10_000
    assert parse_installs(5000) == 5000
    assert parse_installs(None) is None and parse_installs("") is None and parse_installs(True) is None


def test_metrics_find_unhappy_and_abandoned_apps():
    results = [{"appId": f"a{i}", "score": s, "installs": "100,000+"}
               for i, s in enumerate([3.2, 3.8, 4.6, 4.5, 3.9, 4.9])]
    details = {"a0": {"realInstalls": 2_500_000, "updated": ts(800)},
               "a1": {"updated": ts(500)}, "a2": {"updated": ts(10)}, "a3": {"updated": ts(20)}}
    m = playstore_metrics(results, details, today=TODAY, top_n=5)
    assert m["ps_results"] == 6 and m["ps_top_installs"] == 2_500_000
    assert m["ps_low_rated_share"] == 0.6           # 3.2, 3.8, 3.9 of top 5
    assert m["ps_stale_share"] == 0.5               # 2 of 4 apps with a date
    assert m["ps_gap"] == 55.0
    assert m["ps_top_rating"] == round((3.2 + 3.8 + 4.6 + 4.5 + 3.9) / 5, 2)


def test_metrics_no_apps_is_a_wide_open_gap():
    assert playstore_metrics([], {}, today=TODAY) == {"ps_results": 0.0, "ps_gap": 100.0}


async def test_collector_targets_refresh_and_isolation():
    cfg = make_config(markets=["en-US"], playstore={"max_queries_per_market": 10,
                                                    "refresh_days": 7, "top_apps": 2})
    from ode.config import Secrets
    secrets = Secrets(_env_file=None, database_url="x")  # type: ignore[call-arg]
    today = datetime.now(UTC).date()
    state = InMemoryState(
        last={("en-US", "playstore", "ps_results"): {"seed:n8n": today - timedelta(days=2)}},
        top={"en-US": [Probe("wd:Q1", "Heat pump (device)"), Probe("wd:Q2", "Broken")]},
    )
    calls: list[tuple] = []

    def search(q, n_hits, lang, country):
        calls.append(("search", q, lang, country))
        if q == "Broken":
            raise RuntimeError("play markup changed")
        return [{"appId": "x1", "score": 3.5, "installs": "5,000+"}, {"appId": "x2"}]

    def app(app_id, lang, country):
        if app_id == "x2":
            raise RuntimeError("detail failed")  # one app failing must not sink the topic
        return {"realInstalls": 7000, "updated": ts(1)}

    client = httpx.AsyncClient()
    c = PlayStoreCollector(cfg, secrets, client, QuotaTracker(InMemoryQuotaStore(), {}), state,
                           search_fn=search, app_fn=app)
    c.http.bucket.rate = 10_000
    try:
        sigs = await c.collect(cfg.market("en-US"), datetime.now(UTC))
    finally:
        await client.aclose()
    queried = [q for kind, q, *_ in calls if kind == "search"]
    assert "n8n automation" not in queried                 # checked 2 days ago: not due
    assert "raspberry pi project" in queried and "Heat pump" in queried  # disambiguation cut
    assert all(lang == "en" and country == "us" for _, _, lang, country in calls)
    keys = {s.topic_key for s in sigs}
    assert keys == {"seed:raspberry_pi", "wd:Q1"} and c.errors == 1
    assert {s.metric_name for s in sigs} >= {"ps_results", "ps_gap", "ps_top_installs"}
