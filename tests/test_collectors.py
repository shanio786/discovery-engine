from datetime import UTC, date, datetime

import httpx
import pytest
import respx

from ode.collectors.hackernews import parse_hn
from ode.collectors.reddit import parse_reddit
from ode.collectors.wikipedia import WikipediaCollector, parse_pageviews
from ode.collectors.youtube import (
    build_signals,
    iso8601_duration_to_seconds,
)
from ode.quota import InMemoryQuotaStore, QuotaTracker


# ---------- Wikipedia ----------
def test_parse_pageviews():
    payload = {
        "items": [
            {"timestamp": "2024010100", "views": 100, "project": "en.wikipedia"},
            {"timestamp": "2024010200", "views": 150, "project": "en.wikipedia"},
            {"timestamp": "bad", "views": 1},          # skipped
            {"timestamp": "2024010300"},                 # no views -> skipped
        ]
    }
    sigs = parse_pageviews(payload, "en-US", "Raspberry_Pi")
    assert len(sigs) == 2
    assert sigs[0].metric_name == "pageviews"
    assert sigs[0].observed_at == date(2024, 1, 1)
    assert sigs[0].value == 100.0
    assert sigs[0].topic_surface_form == "Raspberry Pi"  # underscores normalized


def test_parse_pageviews_empty():
    assert parse_pageviews(None, "en-US", "X") == []
    assert parse_pageviews({}, "en-US", "X") == []


# ---------- HackerNews ----------
def test_parse_hn_aggregates_by_day():
    d1 = int(datetime(2024, 1, 1, 12, tzinfo=UTC).timestamp())
    d1b = int(datetime(2024, 1, 1, 20, tzinfo=UTC).timestamp())
    d2 = int(datetime(2024, 1, 2, 9, tzinfo=UTC).timestamp())
    payload = {"hits": [
        {"created_at_i": d1}, {"created_at_i": d1b}, {"created_at_i": d2},
        {"created_at_i": None},  # skipped
    ]}
    sigs = parse_hn(payload, "en-US", "self hosting")
    counts = {s.observed_at: s.value for s in sigs}
    assert counts[date(2024, 1, 1)] == 2.0
    assert counts[date(2024, 1, 2)] == 1.0
    assert all(s.metric_name == "hn_mentions" for s in sigs)


# ---------- Reddit ----------
def test_parse_reddit_mentions_and_unanswered():
    c1 = datetime(2024, 1, 1, tzinfo=UTC).timestamp()
    payload = {"data": {"children": [
        {"data": {"created_utc": c1, "num_comments": 0}},
        {"data": {"created_utc": c1, "num_comments": 5}},
        {"data": {"created_utc": c1, "num_comments": 0}},
    ]}}
    sigs = parse_reddit(payload, "en-US", "n8n")
    mentions = [s for s in sigs if s.metric_name == "reddit_mentions"]
    unanswered = [s for s in sigs if s.metric_name == "reddit_unanswered"]
    assert mentions[0].value == 3.0
    assert unanswered[0].value == 2.0


# ---------- YouTube ----------
@pytest.mark.parametrize("value,expected", [
    ("PT12M30S", 750),
    ("PT1H", 3600),
    ("PT1H2M3S", 3723),
    ("PT45S", 45),
    ("PT", None),
    ("", None),
    (None, None),
    ("garbage", None),
])
def test_iso8601_duration(value, expected):
    assert iso8601_duration_to_seconds(value) == expected


def test_build_signals_outlier_and_counts():
    videos = {
        "v1": {"channel_id": "c1", "title": "t1", "views": 50000,
               "published_at": None, "duration": 600},
        "v2": {"channel_id": "c2", "title": "t2", "views": 9_000_000,
               "published_at": None, "duration": 600},  # big channel -> no ratio
    }
    channels = {
        "c1": {"title": "small", "subs": 1000, "total_views": 1, "country": "US",
               "created_at": None},                       # 50000/1000 = 50x outlier
        "c2": {"title": "huge", "subs": 5_000_000, "total_views": 1, "country": "US",
               "created_at": None},                       # above ceiling, ignored
    }
    sigs = build_signals(videos, channels, market="en-US", query="n8n", observed=date(2024, 1, 1))
    metrics = {s.metric_name: s.value for s in sigs if s.metric_name}
    assert metrics["youtube_max_outlier"] == 50.0
    assert metrics["youtube_video_count"] == 2.0
    entity = [s for s in sigs if not s.metric_name]
    assert len(entity) == 2
    assert any(s.video and s.video["id"] == "v1" for s in entity)


# ---------- Wikipedia collect() end-to-end (mocked HTTP) ----------
def _make_collector(cls, config, secrets):
    client = httpx.AsyncClient()
    quota = QuotaTracker(InMemoryQuotaStore(), config.quotas)
    return cls(config, secrets, client, quota), client


@respx.mock
async def test_wikipedia_collect_mocked(config, secrets):
    collector, client = _make_collector(WikipediaCollector, config, secrets)
    route = respx.get(url__regex=r"https://wikimedia\.org/api/rest_v1/.*")
    route.mock(return_value=httpx.Response(200, json={
        "items": [{"timestamp": "2024010100", "views": 123, "project": "en.wikipedia"}]
    }))
    market = config.market("en-US")
    since = datetime(2024, 1, 1, tzinfo=UTC)
    try:
        signals = await collector.collect(market, since)
    finally:
        await client.aclose()
    assert len(signals) == 1
    assert signals[0].value == 123.0
    assert signals[0].source == "wikipedia"


@respx.mock
async def test_wikipedia_collect_handles_404(config, secrets):
    collector, client = _make_collector(WikipediaCollector, config, secrets)
    respx.get(url__regex=r"https://wikimedia\.org/.*").mock(
        return_value=httpx.Response(404)
    )
    try:
        signals = await collector.collect(config.market("en-US"),
                                          datetime(2024, 1, 1, tzinfo=UTC))
    finally:
        await client.aclose()
    assert signals == []  # missing article is skipped, not fatal


async def test_disabled_collectors_report_reason(config, secrets):
    # No API key / creds in test secrets -> YouTube & Reddit self-skip.
    from ode.collectors.reddit import RedditCollector
    from ode.collectors.youtube import YouTubeCollector

    client = httpx.AsyncClient()
    quota = QuotaTracker(InMemoryQuotaStore(), config.quotas)
    try:
        yt = YouTubeCollector(config, secrets, client, quota)
        rd = RedditCollector(config, secrets, client, quota)
        yt_enabled, yt_reason = yt.is_enabled()
        rd_enabled, rd_reason = rd.is_enabled()
        assert yt_enabled is False and "YOUTUBE_API_KEY" in yt_reason
        assert rd_enabled is False and "REDDIT_CLIENT_ID" in rd_reason
    finally:
        await client.aclose()
