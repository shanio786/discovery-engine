from ode.discovery import plan_langlinks
from ode.models import Market
from ode.wikimeta import (
    EntityInfo,
    TitleFilter,
    exclusion_reason,
    norm_title,
    parse_entities,
    parse_morelike,
    parse_pageprops,
    parse_top,
)


def test_norm_title():
    assert norm_title(" Raspberry_Pi ") == "Raspberry Pi"


def test_parse_top():
    payload = {"items": [{"articles": [
        {"article": "Main_Page", "views": 900, "rank": 1},
        {"article": "Raspberry_Pi", "views": 50, "rank": 2},
        {"article": None, "views": 1},
        {"article": "X", "views": "bad"},
    ]}]}
    assert parse_top(payload) == [("Main Page", 900), ("Raspberry Pi", 50)]
    assert parse_top(None) == [] and parse_top({"items": None}) == []


def test_parse_pageprops_follows_normalize_and_redirect_chain():
    payload = {"query": {
        "normalized": [{"from": "raspberry_pi", "to": "Raspberry pi"}],
        "redirects": [{"from": "Raspberry pi", "to": "Raspberry Pi"}],
        "pages": [
            {"pageid": 1, "ns": 0, "title": "Raspberry Pi", "pageprops": {"wikibase_item": "Q1"}},
            {"ns": 0, "title": "Nope", "missing": True},
            {"ns": -1, "title": "Special:Search"},
        ],
    }}
    out = parse_pageprops(payload, ["raspberry_pi", "Nope", "Special:Search", "absent"])
    assert out["raspberry_pi"].title == "Raspberry Pi" and out["raspberry_pi"].qid == "Q1"
    assert out["Nope"].missing is True
    assert out["Special:Search"].ns == -1 and out["Special:Search"].qid is None
    assert "absent" not in out


def test_parse_pageprops_redirect_loop_does_not_hang():
    payload = {"query": {"redirects": [{"from": "A", "to": "B"}, {"from": "B", "to": "A"}],
                         "pages": []}}
    assert parse_pageprops(payload, ["A"]) == {}


def test_parse_entities():
    payload = {"entities": {
        "Q1": {
            "id": "Q1",
            "labels": {"en": {"language": "en", "value": "Raspberry Pi"}},
            "claims": {"P31": [
                {"mainsnak": {"datavalue": {"value": {"id": "Q68"}}}},
                {"mainsnak": {"snaktype": "novalue"}},  # no datavalue -> ignored
            ]},
            "sitelinks": {"enwiki": {"title": "Raspberry Pi"}, "dewiki": {"title": "Raspberry Pi"},
                          "commonswiki": {"title": "Category:Raspberry Pi"},
                          "zh_min_nanwiki": {"title": "X"}},
        },
        "Q2": {"id": "Q2", "missing": ""},
    }}
    ents = parse_entities(payload)
    assert set(ents) == {"Q1"}
    e = ents["Q1"]
    assert e.p31 == ("Q68",) and e.label_en == "Raspberry Pi"
    assert e.wiki_langs == ["de", "en", "zh-min-nan"]  # commons is not a language


def test_parse_morelike_keeps_articles_only():
    payload = {"query": {"search": [{"ns": 0, "title": "Arduino"}, {"ns": 14, "title": "Cat"}]}}
    assert parse_morelike(payload) == ["Arduino"]
    assert parse_morelike({}) == []


def test_title_filter():
    f = TitleFilter(["Main Page", "-"], [r"^\d{4}[ _](?!\()", r"^List[ _]of[ _]"])
    assert f.reason("Main_Page")
    assert f.reason("-")
    assert f.reason("2024 Summer Olympics")
    assert f.reason("2024_Summer_Olympics")
    assert f.reason("List of Raspberry Pi models")
    assert f.reason("Raspberry Pi") is None
    assert f.reason("1984 (novel)") is None  # year then '(' is not an event title


def test_exclusion_reason():
    classes = {"Q5": "human"}
    person = EntityInfo("Q9", "Someone", ("Q5",))
    thing = EntityInfo("Q1", "Pi", ("Q68",))
    assert "human" in exclusion_reason(person, classes)
    assert exclusion_reason(thing, classes) is None
    assert exclusion_reason(None, classes) is None


def test_plan_langlinks_uses_each_markets_language():
    markets = [Market.parse("en-US"), Market.parse("de-DE"), Market.parse("ja-JP")]
    ent = EntityInfo("Q1", "Heat pump", (), {"enwiki": "Heat pump", "dewiki": "Wärmepumpe"})
    plan = plan_langlinks(ent, markets, skip_market="en-US")
    assert [(m.code, t) for m, t in plan] == [("de-DE", "Wärmepumpe")]  # no jawiki -> skipped
