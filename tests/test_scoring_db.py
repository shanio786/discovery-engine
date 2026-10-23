"""Scoring end to end on a real TimescaleDB: phases, components, persistence,
no lookahead, probe queue, and the gems report."""

from datetime import date, datetime, timedelta

import numpy as np

from ode.config import load_config
from ode.db import DbState
from ode.models import RawSignal
from ode.scoring import report
from ode.scoring.scorer import Scorer
from tests.conftest import make_config

AS_OF = date(2026, 6, 30)
t = np.arange(180, dtype=float)


def wiki(key, market, name, values, end=AS_OF):
    n = len(values)
    return [RawSignal("wikipedia", market, name, "pageviews", float(v),
                      end - timedelta(days=n - 1 - i), topic_key=key) for i, v in enumerate(values)]


def cfg():
    return make_config(monetization=load_config().monetization,
                       scoring={**load_config().scoring, "probe_top_k": 2})


async def seed_world(db):
    sigs = []
    sigs += wiki("wd:Q1", "en-US", "Heat pump", 100 + 0.01 * t**2)            # breakout
    sigs += wiki("wd:Q1", "de-DE", "Wärmepumpe", [400.0] * 180)               # stable
    sigs += wiki("wd:Q2", "en-US", "Old fad", 900 - 3 * t)                    # declining
    sigs += wiki("wd:Q3", "en-US", "Brand new thing", [300.0] * 30)           # sparse
    sigs += wiki("wd:Q4", "en-US", "Gone quiet", [500.0] * 100,
                 end=AS_OF - timedelta(days=60))                              # stale
    sigs += wiki("wd:Q5", "en-US", "Gadget tracker", 200 + 0.5 * t)           # growing
    sigs += wiki("wd:Q5", "de-DE", "Gadget-Tracker", [150.0] * 180)  # inconsistent langs
    sigs += wiki("seed:raspberry_pi", "en-US", "Raspberry Pi", 1000 + 2 * t)  # growing seed
    for i in range(90):
        d = AS_OF - timedelta(days=i)
        sigs.append(RawSignal("reddit", "en-US", "raspberry pi project", "reddit_mentions", 4.0,
                              d, topic_key="seed:raspberry_pi"))
        if i > 2:
            sigs.append(RawSignal("reddit", "en-US", "raspberry pi project",
                                  "reddit_unanswered", 1.0, d, topic_key="seed:raspberry_pi"))
    sigs += [RawSignal("youtube", "en-US", "raspberry pi project", "youtube_max_outlier", 30.0,
                       AS_OF, topic_key="seed:raspberry_pi"),
             RawSignal("youtube", "en-US", "raspberry pi project", "youtube_total_results",
                       5000.0, AS_OF, topic_key="seed:raspberry_pi"),
             RawSignal("youtube", "en-US", "Gadget tracker", "youtube_total_results",
                       90000.0, AS_OF, topic_key="wd:Q5")]
    await db.upsert_signals(sigs)
    for key, langs, kind in [("wd:Q1", ["en", "de"], "Q11016"), ("wd:Q5", ["en"], None)]:
        await db.pool.execute("UPDATE topics SET wiki_langs=$2, kind=$3, wikidata_qid=$4 "
                              "WHERE topic_key=$1", key, langs, kind, key[3:])
    for market, term, key, origin in [("en-US", "Heat pump", "wd:Q1", "wiki_top"),
                                      ("de-DE", "Wärmepumpe", "wd:Q1", "langlink"),
                                      ("en-US", "Raspberry Pi", "seed:raspberry_pi", "seed")]:
        await db.watchlist_upsert(market, "wiki_article", term, key, origin)


async def by_name(db, market):
    rows = await db.pool.fetch("SELECT * FROM scores WHERE market=$1 AND date=$2", market, AS_OF)
    import json
    return {json.loads(r["details"])["name"]: r for r in rows}


async def test_scoring_end_to_end(db):
    await seed_world(db)
    scorer = Scorer(cfg(), db)
    reports = await scorer.run(AS_OF)
    assert [r["status"] for r in reports] == ["ok", "ok"]

    en = await by_name(db, "en-US")
    assert en["Heat pump"]["phase"] == "BREAKOUT" and en["Heat pump"]["momentum"] == 3.0
    assert en["Old fad"]["phase"] == "DECLINING"
    assert en["Gadget tracker"]["phase"] == "GROWING"
    assert en["Brand new thing"]["phase"] == "UNKNOWN"
    assert "Gone quiet" not in en                                    # stale: not scored
    assert en["Heat pump"]["composite"] > en["Gadget tracker"]["composite"] > \
        en["Old fad"]["composite"]
    assert en["Gadget tracker"]["money_path"] == "APP"               # 'tracker' keyword
    assert en["Heat pump"]["money_path"] == "PRODUCT"                # Wikidata class
    rpi = en["Raspberry Pi"]
    assert rpi["gap"] is not None and rpi["competition"] is not None
    assert rpi["confidence"] > en["Heat pump"]["confidence"]         # more real data
    assert (await by_name(db, "de-DE"))["Wärmepumpe"]["phase"] == "STABLE"

    import json
    assert json.loads(en["Gadget tracker"]["details"])["missing_in"] == ["de"]
    de = await by_name(db, "de-DE")
    assert json.loads(de["Gadget-Tracker"]["details"])["missing_in"] == []  # never its own

    # probes: best non-seed topics without YouTube data (Gadget tracker has it)
    queued = await DbState(db).pending_probes("en-US", 10)
    assert [p.topic_key for p in queued] == ["wd:Q1", "wd:Q2"]
    assert queued[0].query == "Heat pump"

    # monetization table filled
    # 5 topics: Heat pump/Wärmepumpe and the two trackers are shared across languages
    assert await db.pool.fetchval("SELECT count(*) FROM monetization") == 5


async def test_rescoring_is_idempotent_and_has_no_lookahead(db):
    await seed_world(db)
    scorer = Scorer(cfg(), db)
    await scorer.run(AS_OF)
    before = {k: v["composite"] for k, v in (await by_name(db, "en-US")).items()}
    # data that arrives AFTER as_of must not change scores computed as of then
    future = wiki("wd:Q2", "en-US", "Old fad", [1e6] * 30, end=AS_OF + timedelta(days=30))
    await db.upsert_signals(future)
    await scorer.run(AS_OF)
    after = {k: v["composite"] for k, v in (await by_name(db, "en-US")).items()}
    assert after == before
    assert await db.pool.fetchval("SELECT count(*) FROM scores WHERE date=$1", AS_OF) == \
        len(before) + len(await by_name(db, "de-DE"))


async def test_gems_report(db):
    await seed_world(db)
    await Scorer(cfg(), db).run(AS_OF)
    gems = await report.load_gems(db, AS_OF, market="en-US", limit=10)
    names = [g.name for g in gems]
    assert names[0] == "Heat pump" and "Brand new thing" not in names  # UNKNOWN hidden
    with_unknown = await report.load_gems(db, AS_OF, market="en-US", include_unknown=True)
    assert "Brand new thing" in [g.name for g in with_unknown]
    assert all(g.confidence >= 0.5 for g in
               await report.load_gems(db, AS_OF, min_confidence=0.5))
    gaps = await report.cross_language_gaps(db, AS_OF, ["en-US", "de-DE"])
    assert {"name": "Gadget tracker", "missing_in": ["de"]}.items() <= gaps[0].items()
    md = report.render_markdown(gems, gaps, AS_OF)
    assert "UNVALIDATED" in md and "Cross-language gaps" in md and "| 1 | **Heat pump**" in md
    assert "Heat pump" in report.render_table(gems)
    assert report.render_csv(gems).splitlines()[1].startswith("en-US,1,Heat pump,BREAKOUT")
    assert gems[0].why().startswith("breakout (velocity")


async def test_probe_requeue_only_after_refresh(db):
    await seed_world(db)
    scorer = Scorer(cfg(), db)
    await scorer.run(AS_OF)
    state = DbState(db)
    await state.mark_probed("en-US", "wd:Q1")
    await scorer.run(AS_OF)            # just probed: not re-queued
    assert "wd:Q1" not in {p.topic_key for p in await state.pending_probes("en-US", 10)}
    await db.pool.execute("UPDATE probe_queue SET probed_at = $1 WHERE topic_key = 'wd:Q1'",
                          datetime(2020, 1, 1))
    await scorer.run(AS_OF)            # long ago: re-queued
    assert "wd:Q1" in {p.topic_key for p in await state.pending_probes("en-US", 10)}
