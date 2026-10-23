"""Integration tests against a real Postgres/TimescaleDB (ODE_TEST_DATABASE_URL)."""

from datetime import date, timedelta

import pytest

from ode.db import AlreadyRunning
from ode.models import RawSignal, Seed

D = date(2024, 1, 1)


def sig(value, day=D, market="en-US", key="seed:x", metric="pageviews", surface="X"):
    return RawSignal("wikipedia", market, surface, metric, value, day, topic_key=key)


async def count(db, sql, *a):
    return await db.pool.fetchval(sql, *a)


async def test_schema_is_idempotent_and_timescale_features_applied(db):
    again = await db.init_schema()
    assert again == {"001_schema.sql": "ok", "002_hypertables.sql": "ok",
                     "003_discovery.sql": "ok", "004_compression.sql": "ok",
                     "005_scoring.sql": "ok", "006_realtime.sql": "ok",
                     "007_realtime_timescale.sql": "ok"}
    assert await count(db, "SELECT count(*) FROM timescaledb_information.hypertables "
                           "WHERE hypertable_name='topic_metrics'") == 1
    assert await count(db, "SELECT count(*) FROM timescaledb_information.jobs "
                           "WHERE proc_name='policy_compression'") == 1


async def test_bulk_upsert_idempotent_dedups_batch_and_updates(db):
    batch = [sig(1.0), sig(2.0, D + timedelta(days=1)), sig(9.0)]  # dup key: last wins
    assert await db.upsert_signals(batch) == 2
    assert await db.upsert_signals(batch) == 2
    assert await count(db, "SELECT count(*) FROM topic_metrics") == 2
    assert await count(db, "SELECT value FROM topic_metrics WHERE date=$1", D) == 9.0
    await db.upsert_signals([sig(5.0)])
    assert await count(db, "SELECT value FROM topic_metrics WHERE date=$1", D) == 5.0


async def test_topic_key_shares_one_topic_across_markets(db):
    await db.upsert_signals([sig(1.0, market="en-US", surface="Heat pump", key="wd:Q1"),
                             sig(2.0, market="de-DE", surface="Wärmepumpe", key="wd:Q1")])
    assert await count(db, "SELECT count(*) FROM topics") == 1
    assert await count(db, "SELECT count(DISTINCT topic_id) FROM topic_metrics") == 1
    assert await count(db, "SELECT count(*) FROM topic_aliases") == 2


async def test_entity_only_signal_with_unknown_channel_does_not_break_batch(db):
    video_only = RawSignal("youtube", "en-US", "q", "", 0.0, D, topic_key="seed:q",
                           video={"id": "v1", "channel_id": None, "title": "t", "views": 5})
    assert await db.upsert_signals([video_only, sig(3.0, key="seed:q", surface="q")]) == 1
    assert await count(db, "SELECT count(*) FROM videos") == 1


async def test_seed_merges_previously_discovered_entity(db):
    # Discovery found Q42 first and collected history under 'wd:Q42' ...
    key = await db.ensure_topic_for_qid("Q42", "Raspberry Pi", ["en", "de"])
    assert key == "wd:Q42"
    await db.upsert_signals([sig(7.0, key=key), sig(8.0, key=key, market="de-DE")])
    await db.watchlist_upsert("en-US", "wiki_article", "Raspberry Pi", key, "wiki_top")
    # ... then the operator adds it as a seed: history must move, not split.
    seed = Seed("raspberry_pi", "Raspberry Pi", "Raspberry Pi")
    assert await db.ensure_seed_topic(seed, "Q42", ["en", "de"]) == "seed:raspberry_pi"
    rows = await db.pool.fetch("SELECT topic_key, wikidata_qid FROM topics")
    assert [(r["topic_key"], r["wikidata_qid"]) for r in rows] == [("seed:raspberry_pi", "Q42")]
    assert await count(db, "SELECT count(*) FROM topic_metrics") == 2
    assert await count(db, "SELECT topic_key FROM watchlist") == "seed:raspberry_pi"
    # and later discoveries of Q42 resolve to the seed
    assert await db.ensure_topic_for_qid("Q42", "x", None) == "seed:raspberry_pi"


async def test_watchlist_rules(db):
    W, today = "wiki_article", date(2024, 5, 1)
    assert await db.watchlist_upsert("en-US", W, "A", "wd:Q1", "wiki_top", today=today) is True
    # same topic under another title in the same market -> not duplicated
    assert await db.watchlist_upsert("en-US", W, "A (redirect)", "wd:Q1", "wiki_top") is False
    # class-excluded stays excluded when seen again
    await db.watchlist_upsert("en-US", W, "Person", "wd:Q5x", "wiki_top", active=False,
                              excluded_reason="class Q5 (human)", today=today)
    await db.watchlist_upsert("en-US", W, "Person", "wd:Q5x", "wiki_top", today=today)
    rows = await db.watchlist_rows("en-US", W)
    assert rows["Person"]["active"] is False and rows["A"]["active"] is True
    assert set(rows) == {"A", "Person"}
    # seed wins over a discovered row with the same title
    await db.watchlist_upsert("en-US", W, "A", "seed:a", "seed", today=today)
    assert (await db.watchlist_rows("en-US", W))["A"]["origin"] == "seed"


async def test_eviction_stale_cap_and_revival(db):
    W, today = "wiki_article", date(2024, 5, 1)
    old = today - timedelta(days=60)
    await db.watchlist_upsert("en-US", W, "Seed", "seed:s", "seed", today=old)
    await db.watchlist_upsert("en-US", W, "Old", "wd:Q1", "wiki_top", today=old)
    for i in range(3):
        await db.watchlist_upsert("en-US", W, f"New{i}", f"wd:Q{10 + i}", "wiki_top",
                                  today=today - timedelta(days=i))
    res = await db.watchlist_evict("en-US", W, cap=3, stale_after_days=30, today=today)
    assert res == {"stale": 1, "evicted_cap": 1}
    active = {i.term for i in await db.watchlist_active("en-US", W)}
    assert active == {"Seed", "New0", "New1"}  # seed exempt even though old
    # a stale topic seen again is revived
    await db.watchlist_upsert("en-US", W, "Old", "wd:Q1", "wiki_top", today=today)
    assert "Old" in {i.term for i in await db.watchlist_active("en-US", W)}


async def test_last_dates_by_topic_key(db):
    await db.upsert_signals([sig(1.0, D), sig(1.0, D + timedelta(days=4)),
                             sig(1.0, D, key="seed:y", surface="Y")])
    assert await db.last_dates("en-US", "wikipedia", "pageviews") == {
        "seed:x": D + timedelta(days=4), "seed:y": D}


async def test_harvest_lock_blocks_concurrent_runs(db):
    async with db.harvest_lock():
        with pytest.raises(AlreadyRunning):
            async with db.harvest_lock():
                pass
    async with db.harvest_lock():  # released again
        pass


async def test_orphan_runs_are_closed(db):
    await db.start_run("wikipedia", "en-US")
    assert await db.close_orphan_runs() == 1
    assert await count(db, "SELECT status FROM collector_runs") == "error"


async def test_merge_works_on_compressed_chunks(db):
    key = await db.ensure_topic_for_qid("Q7", "Old topic", None)
    old_day = date(2023, 1, 1)
    await db.upsert_signals([sig(3.0, old_day, key=key), sig(4.0, old_day, key="seed:z",
                                                            surface="Z")])
    await db.pool.execute("SELECT compress_chunk(c, if_not_compressed => true) "
                          "FROM show_chunks('topic_metrics', older_than => INTERVAL '90 days') c")
    await db.ensure_seed_topic(Seed("z", "Z"), "Q7")
    assert await count(db, "SELECT count(*) FROM topic_metrics") == 1  # conflict kept seed's row
    assert await count(db, "SELECT count(*) FROM topics") == 1
    # upsert into a compressed chunk still works
    await db.upsert_signals([sig(5.0, old_day, key="seed:z", surface="Z")])
    assert await count(db, "SELECT value FROM topic_metrics") == 5.0
