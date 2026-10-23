"""Plan + briefs on a real DB (Gemini mocked with respx)."""

import json

import httpx
import pytest
import respx

from ode.briefs import build_prompt, generate_briefs, parse_brief
from ode.config import Secrets, load_config
from ode.pipeline import http_client
from ode.scoring import plan as planmod
from ode.scoring.actions import Action
from ode.scoring.scorer import Scorer
from tests.conftest import make_config
from tests.test_scoring_db import AS_OF, seed_world


def cfg():
    real = load_config()
    return make_config(monetization=real.monetization,
                       scoring={**real.scoring, "probe_top_k": 2},
                       market_value=real.raw()["market_value"],
                       actions={"top_n": 20, "per_market_max": 10},
                       gemini={"model": "gemini-test", "max_briefs_per_day": 2,
                               "reuse_days": 14})


def gemini_reply(titles):
    body = {"titles": titles, "outline": "- hook\n- payoff", "format": "blog",
            "monetization": "PRODUCT: template", "hours": 6}
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(body)}]}}]}


def action(**kw):
    base = {"platform": "BLOG", "market": "de-DE", "language": "de", "topic": "Wärmepumpe", "score": 50,
                "ease": 80, "value": 60, "why": ["🚀 BREAKOUT"], "steps": ["a"], "unverified": [], "topic_id": 1}
    base.update(kw)
    return Action(**base)


def test_prompt_states_language_platform_and_rules():
    p = build_prompt(action())
    assert "German" in p and "Blog / website" in p and "Wärmepumpe" in p
    assert "NO affiliate" in p and "NO SaaS" in p


def test_parse_brief_variants():
    b = parse_brief(gemini_reply([f"T{i}" for i in range(12)]))
    assert len(b["titles"]) == 10 and "**Estimated hours:** 6" in b["outline"]
    fenced = {"candidates": [{"content": {"parts": [{"text": "```json\n" + json.dumps(
        {"titles": ["x"], "outline": "o"}) + "\n```"}]}}]}
    assert parse_brief(fenced)["titles"] == ["x"]
    with pytest.raises(ValueError):
        parse_brief(gemini_reply([]))
    with pytest.raises(ValueError):
        parse_brief({"candidates": []})


async def test_plan_end_to_end(db):
    await seed_world(db)
    config = cfg()
    await Scorer(config, db).run(AS_OF)
    plan = await planmod.build_plan(db, config, AS_OF)
    assert plan, "expected actions"
    assert plan[0].topic == "Heat pump" and plan[0].market == "en-US"
    assert all(a.topic not in ("Old fad", "Brand new thing") for a in plan)  # declining/unknown
    cross = [a for a in plan if a.source_market]
    assert any(a.topic == "Gadget tracker" and a.market == "de-DE" and a.language == "de"
               for a in cross)                                   # proven in en, missing in de
    assert any(a.platform == "APP" and a.topic == "Gadget tracker" for a in plan)
    md = planmod.render_plan_markdown(plan, AS_OF)
    assert md.startswith(f"# Aaj kya karna hai — {AS_OF}") and "**Pehle steps:**" in md
    assert "Heat pump" in planmod.render_plan_table(plan)
    json.dumps(planmod.plan_as_dicts(plan), default=str)          # serializable


async def test_plan_uses_play_store_metrics(db):
    await seed_world(db)
    config = cfg()
    from ode.models import RawSignal
    await db.upsert_signals([
        RawSignal("playstore", "en-US", "Gadget tracker", m, v, AS_OF, topic_key="wd:Q5")
        for m, v in {"ps_gap": 90.0, "ps_top_rating": 3.1, "ps_top_installs": 5e6}.items()])
    await Scorer(config, db).run(AS_OF)
    plan = await planmod.build_plan(db, config, AS_OF)
    app = next(a for a in plan if a.platform == "APP" and a.market == "en-US")
    assert app.ease == 90 and any("3.1★" in w for w in app.why)


@respx.mock
async def test_briefs_cap_reuse_and_skip(db):
    await seed_world(db)
    config = cfg()
    await Scorer(config, db).run(AS_OF)
    plan = await planmod.build_plan(db, config, AS_OF)
    route = respx.post(url__regex=r"https://generativelanguage\.googleapis\.com/.*").mock(
        return_value=httpx.Response(200, json=gemini_reply(["Titel 1", "Titel 2"])))
    no_key = Secrets(_env_file=None, database_url="x")  # type: ignore[call-arg]
    key = Secrets(_env_file=None, database_url="x", gemini_api_key="G")  # type: ignore[call-arg]
    async with http_client(config) as client:
        assert (await generate_briefs(db, config, no_key, plan, client))["status"] == "skipped"
        first = await generate_briefs(db, config, key, plan, client)
        second = await generate_briefs(db, config, key, plan, client)
    assert first == {"status": "ok", "made": 2, "errors": 0}
    assert second["made"] == 0 and route.call_count == 2          # daily cap respected
    assert route.calls[0].request.headers["x-goog-api-key"] == "G"
    assert "key=" not in str(route.calls[0].request.url)          # key never in the URL
    briefs = await planmod.attach_briefs(db, plan)
    assert briefs and briefs[0]["titles"] == ["Titel 1", "Titel 2"]
    md = planmod.render_plan_markdown(plan, AS_OF, briefs)
    assert "Title ideas (Gemini)" in md and "Titel 1" in md
