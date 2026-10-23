"""Language packs: prompt, parsing, job (Gemini + Autocomplete mocked), display."""

import json

import httpx
import pytest
import respx

from ode import langpacks
from ode.config import Secrets
from ode.miners import SUGGEST
from ode.pipeline import http_client
from ode.scoring import plan as planmod
from ode.scoring.scorer import Scorer
from tests.test_plan_briefs import action, cfg
from tests.test_scoring_db import AS_OF, seed_world


def gemini(packs):
    return {"candidates": [{"content": {"parts": [{"text": json.dumps({"packs": packs})}]}}]}


def test_prompt_and_parse():
    a = action(language="en", topic="Heat pump", questions=["how does a heat pump work"])
    p = langpacks.build_prompt(a, ["de", "ja"], ["Heat pumps explained"])
    assert "de (German), ja (Japanese)" in p and "how does a heat pump work" in p
    assert "not a" in p and "no affiliate" in p
    got = langpacks.parse_packs(gemini([
        {"language": "de-DE", "keyword": "wärmepumpe kosten", "titles": ["T1", " "],
         "questions": ["Q1"]},
        {"language": "fr", "keyword": "x", "titles": ["not asked"]},
        {"language": "ja", "titles": []}]), ["de", "ja"])
    assert got == {"de": {"keyword": "wärmepumpe kosten", "titles": ["T1"], "questions": ["Q1"]}}
    with pytest.raises(ValueError):
        langpacks.parse_packs({"candidates": []}, ["de"])


class _Day:
    @staticmethod
    def now(tz=None):
        from datetime import datetime
        return datetime(AS_OF.year, AS_OF.month, AS_OF.day, 7, tzinfo=tz)


async def test_job_end_to_end(db, monkeypatch):
    monkeypatch.setattr(langpacks, "datetime", _Day)
    await seed_world(db)
    config = cfg()
    config.raw()["rate_limits"].update(gemini={"rate": 1e4, "burst": 1e4},
                                       autocomplete={"rate": 1e4, "burst": 1e4})
    config.raw()["langpacks"] = {"max_items": 50}
    await Scorer(config, db).run(AS_OF)
    secrets = Secrets(_env_file=None, database_url="x", gemini_api_key="G")  # type: ignore[call-arg]

    def reply(request):
        asked = json.loads(request.content)["contents"][0]["parts"][0]["text"]
        langs = ["de"] if "de (German)" in asked else ["en"]
        return httpx.Response(200, json=gemini([
            {"language": lg, "keyword": "wärmepumpe" if lg == "de" else "heat pump",
             "titles": [f"{lg} title {i}" for i in range(10)],
             "questions": [f"{lg} q{i}" for i in range(8)]} for lg in langs]))
    with respx.mock() as mock:
        mock.post(url__regex=r"https://generativelanguage.*").mock(side_effect=reply)
        ac = mock.get(SUGGEST).mock(side_effect=lambda r: httpx.Response(
            200, json=[r.url.params["q"], [r.url.params["q"] + " kosten"]]))
        async with http_client(config) as client:
            r = await langpacks.langpacks_job(db, config, secrets, client)
            again = await langpacks.langpacks_job(db, config, secrets, client)
        assert ac.calls and ac.calls[0].request.url.params["gl"] in ("de", "us")
    assert r["status"] == "ok" and r["packs"] >= 2 and r["verified"] == r["packs"]
    assert again["packs"] == 0                                     # reused, no new calls
    plan = await planmod.build_plan(db, config, AS_OF)
    item = next(a for a in plan if a.languages)
    lp = item.languages[0]
    assert lp["titles"][0].endswith("title 0") and lp["real_searches"]
    md = planmod.render_plan_markdown(plan, AS_OF)
    assert " pack** — keyword:" in md and "Google par asli" in md


async def test_job_skips_without_gemini(db):
    async with http_client(cfg()) as client:
        r = await langpacks.langpacks_job(db, cfg(), Secrets(_env_file=None, database_url="x"),  # type: ignore[call-arg]
                                          client)
    assert r == {"status": "skipped", "reason": "GEMINI_API_KEY not set"}
