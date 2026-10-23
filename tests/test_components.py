from datetime import date

import pytest

from ode.config import load_config
from ode.scoring.components import (
    completed_months,
    composite_score,
    confidence_score,
    dominant_share,
    gap_score,
    log_normalize,
    money_profile,
    saturation_forecast,
    weighted_mean,
)
from ode.scoring.scorer import clean_query

MONEY_CFG = load_config().monetization


def test_log_normalize_is_robust_and_per_corpus():
    out = log_normalize({"a": 10, "b": 100, "c": 1000, "giant": 10_000_000})
    assert out["giant"] == 100.0 and out["a"] == 0.0
    assert 0 < out["b"] < out["c"] < 100      # the giant doesn't squash everyone to ~0
    assert log_normalize({"x": 5, "y": 5}) == {"x": 50.0, "y": 50.0}
    assert log_normalize({}) == {}


def test_weighted_mean_renormalizes_over_available():
    w = {"wikipedia": 0.5, "reddit": 0.3, "hackernews": 0.2}
    assert weighted_mean({"wikipedia": 80, "reddit": None, "hackernews": None}, w) == 80
    assert weighted_mean({"wikipedia": 100, "reddit": 0, "hackernews": None}, w) == pytest.approx(62.5)
    assert weighted_mean({"wikipedia": None}, w) is None


def test_money_paths_no_affiliate_no_saas_no_selling():
    plain = money_profile(["Some topic"], kind=None, audience=None, config=MONEY_CFG)
    assert plain.primary == "ADS" and set(plain.paths) == {"ADS"} and not plain.tutorial
    tut = money_profile(["docker compose tutorial"], kind=None, audience=None, config=MONEY_CFG)
    assert tut.primary == "ADS" and tut.tutorial and tut.score > plain.score  # higher ad RPM
    app = money_profile(["habit tracker"], kind=None, audience=None, config=MONEY_CFG)
    assert app.primary == "APP"
    b2b = money_profile(["Power BI"], kind=None, audience="b2b", config=MONEY_CFG)
    assert b2b.primary == "SPONSOR" and b2b.score > 95
    software = money_profile(["Obsidian"], kind="Q7397", audience=None, config=MONEY_CFG)
    assert software.primary == "SPONSOR" and software.paths["SPONSOR"] == "topic type: software"
    for m in (plain, tut, app, b2b, software):
        assert set(m.paths) <= {"SPONSOR", "APP", "ADS"}
    assert "AFFILIATE" not in str(MONEY_CFG).upper() and "PRODUCT" not in MONEY_CFG["enabled_paths"]


def test_product_path_only_when_explicitly_enabled():
    cfg = {**MONEY_CFG, "enabled_paths": ["SPONSOR", "APP", "ADS", "PRODUCT"],
           "rpv": {**MONEY_CFG["rpv"], "PRODUCT": 0.045}}
    m = money_profile(["docker compose tutorial"], kind=None, audience=None, config=cfg)
    assert "PRODUCT" in m.paths


def test_intent_keyword():
    from ode.scoring.components import intent_keyword
    kws = MONEY_CFG["tool_keywords"]
    assert intent_keyword(["BMI calculator"], kws) == "calculator"
    assert intent_keyword(["Zinsrechner"], kws) is None          # whole words only
    assert intent_keyword(["Rechner für Zinsen"], kws) == "rechner"
    assert intent_keyword(["Heat pump"], kws) is None


def test_comparison_intent_is_deprioritised_and_matches_whole_words():
    tut = money_profile(["obsidian plugin tutorial"], kind=None, audience=None, config=MONEY_CFG)
    vs = money_profile(["obsidian vs notion tutorial"], kind=None, audience=None, config=MONEY_CFG)
    assert vs.comparison_penalty and vs.score < tut.score
    testing = money_profile(["unit testing tutorial"], kind=None, audience=None, config=MONEY_CFG)
    assert testing.comparison_penalty is False   # "test" must not match "testing"


def test_german_intent_keywords():
    m = money_profile(["smart home einrichten"], kind=None, audience=None, config=MONEY_CFG)
    assert m.tutorial and m.primary == "ADS"


def test_gap_score_parts():
    g, parts = gap_score(max_outlier=50, top_channel_share=0.2, unanswered_share=0.5)
    assert parts == {"outlier": 100.0, "no_dominant_channel": 100.0, "unanswered": 50.0}
    assert g == pytest.approx(250 / 3)
    owned, p2 = gap_score(max_outlier=None, top_channel_share=1.0, unanswered_share=None)
    assert owned == 0.0 and set(p2) == {"no_dominant_channel"}
    assert gap_score(max_outlier=None, top_channel_share=None, unanswered_share=None) == (None, {})


def test_dominant_share():
    assert dominant_share(["a", "a", "b", "c"]) == 0.5
    assert dominant_share(["a", None]) is None   # too few videos to judge


def test_composite_is_bounded_zero_safe_and_monotonic():
    base = {"multiplier": 1.0, "demand": 60, "money": 50, "gap": 50, "competition": 50}
    c = composite_score(**base)
    assert 0 < c <= 100
    assert composite_score(**{**base, "demand": 0}) > 0           # never collapses to 0
    assert composite_score(**{**base, "multiplier": 3.0}) == pytest.approx(3 * c, rel=1e-3)
    assert composite_score(**{**base, "competition": 90}) < c      # more competition = worse
    assert composite_score(**{**base, "gap": 90}) > c
    missing = composite_score(multiplier=1.0, demand=60, money=50, gap=None, competition=None)
    assert missing == pytest.approx(c, rel=1e-3)                    # missing = neutral


def test_confidence():
    full = confidence_score(points=400, phase="GROWING", demand_sources=2, has_gap=True,
                            has_competition=True, money_specific=True)
    thin = confidence_score(points=90, phase="GROWING", demand_sources=1, has_gap=False,
                            has_competition=False, money_specific=False)
    unknown = confidence_score(points=400, phase="UNKNOWN", demand_sources=2, has_gap=True,
                               has_competition=True, money_specific=True)
    assert full == 1.0 and thin < 0.3 and unknown == 0.3


def test_completed_months():
    assert completed_months(date(2026, 3, 15), 4) == [
        date(2026, 2, 1), date(2026, 1, 1), date(2025, 12, 1), date(2025, 11, 1)]


@pytest.mark.parametrize("recent,old,label,left", [
    (100, 30, "CLOSING_FAST", 2),
    (50, 30, "NARROWING", 6),
    (30, 30, "OPEN", 12),
])
def test_saturation_forecast(recent, old, label, left):
    as_of = date(2026, 6, 15)
    months = completed_months(as_of, 12)
    data = {m: recent for m in months[:3]} | {m: old for m in months[9:12]}
    assert saturation_forecast(data, as_of) == (label, left)


def test_saturation_unknown_without_full_history():
    as_of = date(2026, 6, 15)
    months = completed_months(as_of, 12)
    assert saturation_forecast({m: 10 for m in months[:3]}, as_of) == ("UNKNOWN", None)


def test_clean_query():
    assert clean_query("Obsidian (software)") == "Obsidian"
    assert clean_query("Raspberry Pi") == "Raspberry Pi"
