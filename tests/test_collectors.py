from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx

from ode.collectors.base import BaseCollector
from ode.collectors.hackernews import HackerNewsCollector, day_bounds, parse_nbhits
from ode.collectors.reddit import RedditCollector, aggregate_reddit, parse_posts, search_url
from ode.collectors.wikipedia import WikipediaCollector, fill_gaps, parse_pageviews
from ode.collectors.youtube import YouTubeCollector, build_signals, iso8601_duration_to_seconds
from ode.http import CircuitOpenError
from ode.models import WatchItem
from ode.quota import InMemoryQuotaStore, QuotaTracker
from ode.state import InMemoryState
from tests.conftest import make_config

TODAY = datetime.now(UTC).date()


def make(cls, config, secrets, state=None, quota=None):
    client = httpx.AsyncClient()
    quota = quota or QuotaTracker(InMemoryQuotaStore(), config.quotas)
    return cls(config, secrets, client, quota, state), client


# ======================================================== Wikipedia
def test_parse_pageviews_sorted_and_tolerant():
    payload = {"items": [
        {"timestamp": "2024010200", "views": 150},
        {"timestamp": "2024010100", "views": 100},
        {"timestamp": "bad", "views": 1},
        {"timestamp": "2024010300"},
    ]}
    sigs = parse_pageviews(payload, "en-US", "Raspberry_Pi", "seed:raspberry_pi")
    assert [(s.observed_at, s.value) for s in sigs] == [(date(2024, 1, 1), 100.0),
                                                        (date(2024, 1, 2), 150.0)]
    assert sigs[0].topic_surface_form == "Raspberry Pi"
    assert sigs[0].topic_key == "seed:raspberry_pi"
    assert parse_pageviews(None, "en-US", "X") == []


def test_fill_gaps_only_interior():
    sigs = parse_pageviews({"items": [{"timestamp": "2024010100", "views": 5},
                                      {"timestamp": "2024010400", "views": 7}]}, "en-US", "A")
    filled = fill_gaps(sigs)
    assert [(s.observed_at.day, s.value) for s in filled] == [(1, 5.0), (2, 0.0), (3, 0.0), (4, 7.0)]
    assert filled[1].raw == {"filled": True}
    assert fill_gaps(sigs[:1]) == sigs[:1]


@respx.mock
async def test_wikipedia_incremental_uses_watchlist_and_last_dates(config, secrets):
    last = TODAY - timedelta(days=2)
    state = InMemoryState(
        watch={("en-US", "wiki_article"): [WatchItem("Raspberry Pi", "seed:raspberry_pi"),
                                           WatchItem("Raspberry Pi 5", "seed:raspberry_pi"),
                                           WatchItem("Arduino", "wd:Q1")]},
        last={("en-US", "wikipedia", "pageviews"): {"seed:raspberry_pi": last}},
    )
    route = respx.get(url__regex=r"https://wikimedia\.org/api/rest_v1/.*").mock(
        return_value=httpx.Response(200, json={"items": [
            {"timestamp": f"{TODAY - timedelta(days=1):%Y%m%d}00", "views": 9}]}))
    c, client = make(WikipediaCollector, config, secrets, state)
    try:
        sigs = await c.collect(config.market("en-US"), datetime.now(UTC) - timedelta(days=30))
    finally:
        await client.aclose()
    urls = sorted(str(call.request.url) for call in route.calls)
    assert len(urls) == 2  # duplicate topic (Raspberry Pi 5) tracked once
    rpi = next(u for u in urls if "Raspberry_Pi" in u)
    assert f"/user/Raspberry_Pi/daily/{last - timedelta(days=3):%Y%m%d}/" in rpi  # overlap 3
    ard = next(u for u in urls if "Arduino" in u)
    assert f"/daily/{TODAY - timedelta(days=30):%Y%m%d}/" in ard  # new topic: full lookback
    assert {s.topic_key for s in sigs} == {"seed:raspberry_pi", "wd:Q1"}


@respx.mock
async def test_wikipedia_404_and_failures_are_isolated(config, secrets):
    state = InMemoryState(watch={("en-US", "wiki_article"): [
        WatchItem("Gone", "wd:Q1"), WatchItem("Broken", "wd:Q2"), WatchItem("Fine", "wd:Q3")]})
    respx.get(url__regex=r".*/Gone/.*").mock(return_value=httpx.Response(404))
    respx.get(url__regex=r".*/Broken/.*").mock(return_value=httpx.Response(400))
    respx.get(url__regex=r".*/Fine/.*").mock(return_value=httpx.Response(200, json={
        "items": [{"timestamp": "2024010100", "views": 3}]}))
    c, client = make(WikipediaCollector, config, secrets, state)
    try:
        sigs = await c.collect(config.market("en-US"), datetime.now(UTC) - timedelta(days=30))
    finally:
        await client.aclose()
    assert [s.topic_key for s in sigs] == ["wd:Q3"]
    assert c.errors == 1 and c.partial


@respx.mock
async def test_wikipedia_falls_back_to_seed_titles_when_watchlist_empty(config, secrets):
    route = respx.get(url__regex=r"https://wikimedia\.org/.*").mock(
        return_value=httpx.Response(404))
    c, client = make(WikipediaCollector, config, secrets, InMemoryState())
    try:
        await c.collect(config.market("en-US"), datetime.now(UTC) - timedelta(days=5))
        await c.collect(config.market("de-DE"), datetime.now(UTC) - timedelta(days=5))
    finally:
        await client.aclose()
    assert route.call_count == 1  # en only; de has no fallback (would be an English title)


# ======================================================== Hacker News
def test_parse_nbhits():
    assert parse_nbhits({"nbHits": 12}) == 12
    assert parse_nbhits({"nbHits": 0}) == 0
    for bad in (None, {}, {"nbHits": -1}, {"nbHits": "3"}, {"nbHits": True}):
        assert parse_nbhits(bad) is None


def test_day_bounds_is_utc_day():
    t0, t1 = day_bounds(date(2024, 1, 1))
    assert t0 == int(datetime(2024, 1, 1, tzinfo=UTC).timestamp()) and t1 - t0 == 86_400


def test_hn_attributed_to_one_market_only(config, secrets):
    c = HackerNewsCollector(config, secrets, httpx.AsyncClient(), QuotaTracker(
        InMemoryQuotaStore(), {}))
    assert c.supports(config.market("en-US"))[0] is True
    assert c.supports(config.market("de-DE"))[0] is False
    multi = make_config(markets=["en-US", "en-GB"])
    c2 = HackerNewsCollector(multi, secrets, httpx.AsyncClient(), QuotaTracker(
        InMemoryQuotaStore(), {}))
    assert c2.supports(multi.market("en-GB"))[0] is False  # no duplicate English signal


@respx.mock
async def test_hn_counts_each_day_exactly_and_never_writes_fake_zero(config, secrets):
    state = InMemoryState(last={("en-US", "hackernews", "hn_mentions"): {
        "seed:raspberry_pi": TODAY - timedelta(days=1), "seed:n8n": TODAY - timedelta(days=1)}})

    def answer(request):
        if request.url.params["query"] == "n8n":
            return httpx.Response(200, json={"oops": True})
        return httpx.Response(200, json={"nbHits": 4, "hits": []})

    route = respx.get(url__regex=r"https://hn\.algolia\.com/.*").mock(side_effect=answer)
    c, client = make(HackerNewsCollector, config, secrets, state)
    try:
        sigs = await c.collect(config.market("en-US"), datetime.now(UTC) - timedelta(days=5))
    finally:
        await client.aclose()
    # last=yesterday, overlap 2 -> days today-3 .. today = 4 days per seed
    assert route.call_count == 8
    assert all(call.request.url.params["hitsPerPage"] == "0" for call in route.calls)
    assert {s.observed_at for s in sigs} == {TODAY - timedelta(days=i) for i in range(4)}
    assert all(s.topic_key == "seed:raspberry_pi" and s.value == 4 for s in sigs)
    assert c.errors == 4  # n8n's unusable payloads are errors, not zeros


# ======================================================== Reddit
NOW = datetime(2024, 3, 10, 12, tzinfo=UTC)


def post(pid, days_ago, comments=0, hours=0):
    return {"id": pid, "created": NOW - timedelta(days=days_ago, hours=hours),
            "num_comments": comments}


def test_parse_posts_and_cursor():
    payload = {"data": {"after": "t3_z", "children": [
        {"data": {"name": "t3_a", "created_utc": NOW.timestamp(), "num_comments": 2}},
        {"data": {"name": "t3_b"}},  # no timestamp -> skipped
    ]}}
    posts, after = parse_posts(payload)
    assert after == "t3_z" and [p["id"] for p in posts] == ["t3_a"]


def test_search_url_per_scope():
    assert search_url({"mode": "global"}) == ("https://oauth.reddit.com/search", {})
    url, extra = search_url({"subreddits": ["de", "de_EDV"]})
    assert url == "https://oauth.reddit.com/r/de+de_EDV/search" and extra == {"restrict_sr": "1"}


def test_aggregate_drops_partially_seen_oldest_day():
    posts = [post("a", 0), post("b", 1), post("c", 3)]  # page cap hit, not exhausted
    sigs = aggregate_reddit(posts, market="en-US", query="q", topic_key="seed:x",
                            start=NOW.date() - timedelta(days=10), exhausted=False, now=NOW)
    days = sorted({s.observed_at for s in sigs if s.metric_name == "reddit_mentions"})
    assert days[0] == NOW.date() - timedelta(days=2)  # day of "c" (oldest) not written
    assert days[-1] == NOW.date()


def test_aggregate_exhausted_covers_from_start_with_true_zeros():
    start = NOW.date() - timedelta(days=4)
    sigs = aggregate_reddit([post("a", 1, comments=0), post("a", 1)], market="en-US", query="q",
                            topic_key=None, start=start, exhausted=True, now=NOW)
    m = {s.observed_at: s.value for s in sigs if s.metric_name == "reddit_mentions"}
    assert len(m) == 5 and m[NOW.date() - timedelta(days=1)] == 1.0  # dup id counted once
    assert m[start] == 0.0


def test_aggregate_unanswered_only_judged_when_old_enough():
    posts = [post("new", 0, hours=1), post("mid", 1), post("old", 3), post("old2", 3, comments=4)]
    sigs = aggregate_reddit(posts, market="en-US", query="q", topic_key=None,
                            start=NOW.date() - timedelta(days=5), exhausted=True, now=NOW,
                            min_age_hours=48)
    u = {s.observed_at: s.value for s in sigs if s.metric_name == "reddit_unanswered"}
    assert NOW.date() not in u and NOW.date() - timedelta(days=1) not in u
    assert u[NOW.date() - timedelta(days=3)] == 1.0


def test_aggregate_without_data_writes_nothing():
    assert aggregate_reddit([], market="en-US", query="q", topic_key=None,
                            start=NOW.date(), exhausted=False, now=NOW) == []


def test_reddit_skips_markets_without_scope(config, secrets):
    c = RedditCollector(config, secrets, httpx.AsyncClient(), QuotaTracker(InMemoryQuotaStore(), {}))
    assert c.supports(config.market("en-US"))[0]
    assert c.supports(make_config(markets=["fr-FR"]).market("fr-FR"))[0] is False


# ======================================================== YouTube
@pytest.mark.parametrize("value,expected", [
    ("PT12M30S", 750), ("PT1H", 3600), ("PT1H2M3S", 3723), ("PT45S", 45),
    ("PT", None), ("", None), (None, None), ("garbage", None),
])
def test_iso8601_duration(value, expected):
    assert iso8601_duration_to_seconds(value) == expected


def test_build_signals_outlier_and_missing_channel():
    videos = {
        "v1": {"channel_id": "c1", "title": "t1", "views": 50_000, "published_at": None,
               "duration": 600},
        "v2": {"channel_id": "c2", "title": "t2", "views": 9_000_000, "published_at": None,
               "duration": 600},
        "v3": {"channel_id": "gone", "title": "t3", "views": 10, "published_at": None,
               "duration": 60},
    }
    channels = {
        "c1": {"title": "small", "subs": 1000, "total_views": 1, "country": "US",
               "created_at": None},
        "c2": {"title": "huge", "subs": 5_000_000, "total_views": 1, "country": "US",
               "created_at": None},
    }
    sigs = build_signals(videos, channels, market="en-US", query="q", observed=date(2024, 1, 1),
                         topic_key="seed:x")
    metrics = {s.metric_name: s.value for s in sigs if s.metric_name}
    assert metrics == {"youtube_max_outlier": 50.0, "youtube_video_count": 3.0}
    v3 = next(s for s in sigs if s.video and s.video["id"] == "v3")
    assert v3.video["channel_id"] is None and v3.channel is None  # no FK violation
    assert all(s.topic_key == "seed:x" for s in sigs)


def test_build_signals_zero_videos_still_records_count():
    sigs = build_signals({}, {}, market="en-US", query="q", observed=date(2024, 1, 1))
    assert [(s.metric_name, s.value) for s in sigs] == [("youtube_video_count", 0.0)]


async def test_youtube_rotation_least_recent_first(config, secrets):
    s = Secrets_with_key(secrets)
    state = InMemoryState(last={("en-US", "youtube", "youtube_video_count"): {
        "seed:raspberry_pi": TODAY}})
    c, client = make(YouTubeCollector, config, s, state)
    try:
        order = [seed.key for seed, _ in await c._ordered_queries(config.market("en-US"))]
    finally:
        await client.aclose()
    assert order == ["n8n", "raspberry_pi"]  # never-searched n8n goes first


@respx.mock
async def test_youtube_stops_cleanly_at_quota(config, secrets):
    respx.get(url__regex=r"https://www\.googleapis\.com/.*").mock(
        return_value=httpx.Response(200, json={"items": []}))
    quota = QuotaTracker(InMemoryQuotaStore(), {"youtube": 150})  # one search only
    c, client = make(YouTubeCollector, config, Secrets_with_key(secrets), InMemoryState(), quota)
    try:
        sigs = await c.collect(config.market("en-US"), datetime.now(UTC) - timedelta(days=30))
    finally:
        await client.aclose()
    assert c.last_quota_used == 100
    assert any("quota" in n for n in c.notes) and c.partial
    assert [s.metric_name for s in sigs] == ["youtube_video_count"]


def Secrets_with_key(secrets):
    return secrets.model_copy(update={"youtube_api_key": "K"})


# ======================================================== shared behaviour
async def test_disabled_collectors_report_reason(config, secrets):
    client = httpx.AsyncClient()
    quota = QuotaTracker(InMemoryQuotaStore(), config.quotas)
    try:
        yt_ok, yt_reason = YouTubeCollector(config, secrets, client, quota).is_enabled()
        rd_ok, rd_reason = RedditCollector(config, secrets, client, quota).is_enabled()
    finally:
        await client.aclose()
    assert yt_ok is False and "YOUTUBE_API_KEY" in yt_reason
    assert rd_ok is False and "REDDIT_CLIENT_ID" in rd_reason


class _Dummy(BaseCollector):
    name = "wikipedia"

    async def collect(self, market, since):  # pragma: no cover - unused
        return []


async def test_map_items_isolates_failures_and_circuit_keeps_results(config, secrets):
    c, client = make(_Dummy, config, secrets)

    async def fn(i):
        if i == 2:
            raise ValueError("bad item")
        if i == 4:
            raise CircuitOpenError("host down")
        return i

    c.concurrency = 1
    try:
        out = await c.map_items(range(8), fn)
    finally:
        await client.aclose()
    assert out == [0, 1, 3]          # items after the circuit opened never started
    assert c.errors == 1 and c.aborted == "host down" and c.partial
