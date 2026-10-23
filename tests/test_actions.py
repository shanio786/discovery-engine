from ode.scoring.actions import (
    TopicView,
    plan_topic,
    select_plan,
    supply_demand_ease,
)

VALUES = {"en-US": 1.0, "de-DE": 0.85, "hi-IN": 0.1}
LANGS = {"en": "en-US", "de": "de-DE", "hi": "hi-IN"}


def view(**kw):
    base = {"market": "en-US", "language": "en", "name": "Heat pump", "phase": "BREAKOUT", "momentum": 3.0,
                "demand": 80, "money": 60, "money_path": "PRODUCT",
                "money_paths": {"ADS": "always", "PRODUCT": "keyword 'how to'"}, "gap": None,
                "competition": None, "confidence": 0.5, "saturation": "UNKNOWN", "mean30": 2000,
                "change30": 0.3, "market_value": 1.0}
    base.update(kw)
    return TopicView(**base)


def platforms(actions):
    return {(a.platform, a.market) for a in actions}


def test_supply_demand_ease_is_comparable_across_markets():
    assert supply_demand_ease(0, 1000) == 100.0
    open_field = supply_demand_ease(500, 2000)
    crowded = supply_demand_ease(50_000, 2000)
    assert open_field > 80 and crowded < 40
    assert supply_demand_ease(None, 100) is None and supply_demand_ease(10, 0) is None


def test_dying_or_unmeasured_topics_get_no_actions():
    assert plan_topic(view(phase="DECLINING")) == []
    assert plan_topic(view(phase="UNKNOWN")) == []


def test_breakout_gets_shorts_youtube_blog_product():
    p = platforms(plan_topic(view()))
    assert {("SHORTS", "en-US"), ("YOUTUBE", "en-US"), ("BLOG", "en-US"),
            ("PRODUCT", "en-US")} <= p
    assert ("APP", "en-US") not in p          # no utility intent, no Play data


def test_stable_topic_gets_no_shorts():
    assert "SHORTS" not in {a.platform for a in plan_topic(view(phase="STABLE", momentum=1.0))}


def test_app_action_uses_play_store_gap():
    a = next(x for x in plan_topic(view(money_paths={"ADS": "a", "APP": "utility keyword 'tracker'"},
                                    money_path="APP",
                                    playstore={"ps_gap": 80, "ps_top_rating": 3.4,
                                               "ps_top_installs": 1e6, "ps_stale_share": 0.6}))
         if x.platform == "APP")
    assert a.ease == 80 and not a.unverified
    assert any("3.4★" in w for w in a.why) and any("1,000,000" in w for w in a.why)


def test_cross_language_action_lands_in_target_market():
    acts = plan_topic(view(missing_in=["de"]), market_values=VALUES, language_markets=LANGS)
    de = [a for a in acts if a.market == "de-DE"]
    assert {a.platform for a in de} == {"BLOG", "YOUTUBE"}
    assert all(a.language == "de" and a.source_market == "en-US" and a.ease >= 80 for a in de)
    assert "German" in de[0].headline


def test_market_value_changes_value_not_ease():
    rich = plan_topic(view(market_value=1.0))
    poor = plan_topic(view(market="hi-IN", language="hi", market_value=0.1))
    r = next(a for a in rich if a.platform == "BLOG")
    p = next(a for a in poor if a.platform == "BLOG")
    assert r.value > p.value and r.ease == p.ease


def test_youtube_ease_uses_measured_supply_and_outliers():
    measured = next(a for a in plan_topic(view(competing_videos=300, outlier=90))
                    if a.platform == "YOUTUBE")
    unmeasured = next(a for a in plan_topic(view()) if a.platform == "YOUTUBE")
    assert measured.ease > unmeasured.ease == 50.0
    assert not measured.unverified and unmeasured.unverified


def test_select_plan_spreads_over_markets_and_topics():
    acts = []
    for i in range(6):
        acts += plan_topic(view(name=f"T{i}", demand=90 - i))
    acts += plan_topic(view(market="de-DE", language="de", name="Wärmepumpe", demand=50))
    plan = select_plan(acts, top_n=10, per_market_max=4, per_topic_max=2)
    en = [a for a in plan if a.market == "en-US"]
    assert len(en) == 4 and any(a.market == "de-DE" for a in plan)
    assert max(sum(1 for a in plan if a.topic == t) for t in {a.topic for a in plan}) <= 2
    assert [a.score for a in plan if a.market == "en-US"] == sorted(
        [a.score for a in plan if a.market == "en-US"], reverse=True)


def test_steps_are_concrete_and_never_affiliate():
    for a in plan_topic(view(missing_in=["de"], money_paths={"ADS": "a", "PRODUCT": "p",
                                                             "APP": "u"}),
                        market_values=VALUES, language_markets=LANGS):
        assert len(a.steps) >= 3
        text = " ".join(a.steps).lower()
        assert "affiliate" not in text or "affiliate nahi" in text
