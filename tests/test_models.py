from datetime import date

import pytest

from ode.models import Market, RawSignal


def test_market_parse_basic():
    m = Market.parse("en-US")
    assert m.language == "en"
    assert m.region == "US"
    assert m.wiki_project == "en.wikipedia"


def test_market_parse_override():
    m = Market.parse("pt-BR", {"pt-BR": "pt.wikipedia"})
    assert m.wiki_project == "pt.wikipedia"
    assert m.language == "pt"
    assert m.region == "BR"


def test_market_parse_invalid():
    with pytest.raises(ValueError):
        Market.parse("nonsense")


def test_rawsignal_dedup_key():
    s = RawSignal("wikipedia", "en-US", "Raspberry Pi", "pageviews", 10.0, date(2024, 1, 1))
    assert s.dedup_key() == ("wikipedia", "en-US", "Raspberry Pi", "pageviews", date(2024, 1, 1))


def test_rawsignal_defaults():
    s = RawSignal("reddit", "de-DE", "n8n", "reddit_mentions", 3.0, date(2024, 2, 2))
    assert s.raw == {}
    assert s.video is None and s.channel is None
