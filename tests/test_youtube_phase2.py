from datetime import UTC, datetime, timedelta

import httpx
import respx

from ode.collectors.youtube import YouTubeCollector, parse_total_results
from ode.models import Probe
from ode.quota import InMemoryQuotaStore, QuotaTracker
from ode.scoring.components import completed_months
from ode.state import InMemoryState
from tests.conftest import make_config

TODAY = datetime.now(UTC).date()


def test_parse_total_results():
    assert parse_total_results({"pageInfo": {"totalResults": 1234}}) == 1234
    for bad in (None, {}, {"pageInfo": {}}, {"pageInfo": {"totalResults": "9"}},
                {"pageInfo": {"totalResults": -1}}):
        assert parse_total_results(bad) is None


def make(state, quota_limit=9000, **yt):
    cfg = make_config(markets=["en-US"], youtube={"max_results": 5, "seed_refresh_days": 3,
                                                  "max_probes_per_market": 2,
                                                  "max_month_fills": 3, "saturation_months": 12,
                                                  **yt})
    from ode.config import Secrets
    secrets = Secrets(_env_file=None, database_url="x", youtube_api_key="K")  # type: ignore[call-arg]
    client = httpx.AsyncClient()
    quota = QuotaTracker(InMemoryQuotaStore(), {"youtube": quota_limit})
    return YouTubeCollector(cfg, secrets, client, quota, state), client, cfg


def mock_api():
    def search(request):
        p = request.url.params
        if p.get("videoDuration") == "short":
            return httpx.Response(200, json={"pageInfo": {"totalResults": 11}, "items": []})
        if p.get("maxResults") == "0":
            return httpx.Response(200, json={"pageInfo": {"totalResults": 42}, "items": []})
        return httpx.Response(200, json={"pageInfo": {"totalResults": 777},
                                         "items": [{"id": {"videoId": "v1"}}]})
    respx.get("https://www.googleapis.com/youtube/v3/search").mock(side_effect=search)
    respx.get("https://www.googleapis.com/youtube/v3/videos").mock(return_value=httpx.Response(
        200, json={"items": [{"id": "v1", "statistics": {"viewCount": "5000"},
                              "snippet": {"channelId": "c1", "title": "t"}}]}))
    respx.get("https://www.googleapis.com/youtube/v3/channels").mock(return_value=httpx.Response(
        200, json={"items": [{"id": "c1", "statistics": {"subscriberCount": "100"},
                              "snippet": {"title": "small"}}]}))


@respx.mock
async def test_seeds_then_probes_then_monthly_backfill():
    mock_api()
    state = InMemoryState(
        last={("en-US", "youtube", "youtube_video_count"): {"seed:n8n": TODAY}},  # n8n not due
        probes={"en-US": [Probe("wd:Q1", "Heat pump"), Probe("wd:Q2", "Arduino"),
                          Probe("wd:Q3", "Third")]},
    )
    c, client, _ = make(state)
    try:
        sigs = await c.collect(c.config.market("en-US"), datetime.now(UTC) - timedelta(days=180))
    finally:
        await client.aclose()
    keys = {(s.topic_key, s.metric_name) for s in sigs if s.metric_name}
    assert ("seed:raspberry_pi", "youtube_total_results") in keys      # due seed searched
    assert not any(k == "seed:n8n" for k, _ in keys if _ == "youtube_video_count")
    assert ("wd:Q1", "youtube_max_outlier") in keys and ("wd:Q2", "youtube_total_results") in keys
    assert not any(k == "wd:Q3" for k, _ in keys)                      # probe cap = 2
    assert [p.topic_key for p in await state.probed("en-US")] == ["wd:Q1", "wd:Q2"]
    monthly = [s for s in sigs if s.metric_name == "youtube_monthly_uploads"]
    assert len(monthly) == 3 and all(s.value == 42 for s in monthly)  # fill cap = 3
    assert monthly[0].observed_at == completed_months(TODAY, 1)[0]
    shorts = {s.topic_key: s.value for s in sigs if s.metric_name == "youtube_shorts_total"}
    assert shorts == {"seed:raspberry_pi": 11, "wd:Q1": 11, "wd:Q2": 11}
    assert c.last_quota_used == 102 + 2 * 102 + 3 * 100 + 3 * 100   # + one shorts search each


@respx.mock
async def test_shorts_checks_are_capped_and_optional():
    mock_api()
    state = InMemoryState(probes={"en-US": [Probe("wd:Q1", "Heat pump"),
                                            Probe("wd:Q2", "Arduino")]})
    c, client, _ = make(state, max_shorts_checks=1, max_month_fills=0)
    off, client2, _ = make(InMemoryState(probes={"en-US": [Probe("wd:Q1", "Heat pump")]}),
                           shorts_competition=False, max_month_fills=0)
    try:
        sigs = await c.collect(c.config.market("en-US"), datetime.now(UTC) - timedelta(days=180))
        sigs2 = await off.collect(off.config.market("en-US"),
                                  datetime.now(UTC) - timedelta(days=180))
    finally:
        await client.aclose()
        await client2.aclose()
    assert sum(s.metric_name == "youtube_shorts_total" for s in sigs) == 1
    assert not any(s.metric_name == "youtube_shorts_total" for s in sigs2)


@respx.mock
async def test_monthly_backfill_skips_months_already_stored():
    mock_api()
    months = completed_months(TODAY, 12)
    stored = {"seed:raspberry_pi": set(months[:11]), "seed:n8n": set(months)}
    state = InMemoryState(
        last={("en-US", "youtube", "youtube_video_count"): {
            "seed:raspberry_pi": TODAY, "seed:n8n": TODAY}},
        observed={("en-US", "youtube", "youtube_monthly_uploads"): stored},
    )
    c, client, _ = make(state)
    try:
        sigs = await c.collect(c.config.market("en-US"), datetime.now(UTC) - timedelta(days=180))
    finally:
        await client.aclose()
    assert [(s.topic_key, s.observed_at) for s in sigs] == [("seed:raspberry_pi", months[11])]
    assert c.last_quota_used == 100


@respx.mock
async def test_quota_exhaustion_stops_all_stages_cleanly():
    mock_api()
    state = InMemoryState(probes={"en-US": [Probe("wd:Q1", "Heat pump")]})
    c, client, _ = make(state, quota_limit=150)
    try:
        await c.collect(c.config.market("en-US"), datetime.now(UTC) - timedelta(days=180))
    finally:
        await client.aclose()
    assert c.last_quota_used <= 150 and c.partial
    assert await state.probed("en-US") == []   # never marked probed without data
