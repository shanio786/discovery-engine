"""Postgres/TimescaleDB access layer (asyncpg).

All writes are idempotent so crashed / re-run / overlapping jobs never
duplicate or lose data. Time-series rows are written in bulk (COPY into a
staging table, then one INSERT ... ON CONFLICT), so thousands of topics x
hundreds of days stay fast on a small VPS.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg

from ode.logging import get_logger
from ode.models import Probe, RawSignal, Seed, WatchItem

log = get_logger(__name__)

SQL_DIR = Path(__file__).resolve().parent / "sql"
OPTIONAL_MARKER = "ode:optional"
HARVEST_LOCK_ID = 7_301_995_001  # arbitrary, stable advisory-lock key

_METRIC_COLS = ["topic_id", "market", "date", "source", "metric_name", "value"]
# When one title is reached several ways, the most specific origin label wins.
ORIGIN_PRIORITY = ["langlink", "wiki_top", "trend", "realtime", "morelike", "seed"]


class AlreadyRunning(RuntimeError):
    """Another harvest holds the advisory lock."""


class Database:
    """Thin wrapper around an asyncpg pool with ODE-specific helpers."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str, *, min_size: int = 1, max_size: int = 8) -> Database:
        pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    # ================================================================ schema
    async def init_schema(self) -> dict[str, str]:
        """Apply ode/sql/*.sql in order. Every file is idempotent.

        Files whose first line carries `ode:optional` (TimescaleDB features)
        are best-effort: on failure we log a warning and carry on with plain
        Postgres, so the pipeline still runs. Returns {file: 'ok'|'skipped'}.
        """
        result: dict[str, str] = {}
        for path in sorted(SQL_DIR.glob("*.sql")):
            sql = path.read_text(encoding="utf-8")
            optional = OPTIONAL_MARKER in sql.splitlines()[0] if sql else False
            try:
                async with self.pool.acquire() as con, con.transaction():
                    await con.execute(sql)
                result[path.name] = "ok"
            except asyncpg.PostgresError as exc:
                if not optional:
                    raise
                result[path.name] = "skipped"
                log.warning("schema.optional_skipped", file=path.name, reason=str(exc)[:200])
        log.info("schema.ready", **result)
        return result

    # ================================================================ locking
    @asynccontextmanager
    async def harvest_lock(self) -> AsyncIterator[None]:
        """Session advisory lock: overlapping cron jobs can't run concurrently."""
        con = await self.pool.acquire()
        try:
            got = await con.fetchval("SELECT pg_try_advisory_lock($1)", HARVEST_LOCK_ID)
            if not got:
                raise AlreadyRunning("another ODE harvest is running (advisory lock held)")
            try:
                yield
            finally:
                await con.execute("SELECT pg_advisory_unlock($1)", HARVEST_LOCK_ID)
        finally:
            await self.pool.release(con)

    # ================================================================ topics
    async def resolve_topic(
        self,
        con: asyncpg.Connection,
        market: str,
        surface_form: str,
        topic_key: str | None = None,
    ) -> int:
        """Get-or-create a topic. Returns topic_id.

        With a topic_key (e.g. 'wd:Q42', 'seed:n8n') the topic is shared across
        every market/language; the surface form is recorded as an alias.
        Without one, fall back to one topic per (market, surface form).
        """
        if topic_key:
            topic_id = await con.fetchval("SELECT id FROM topics WHERE topic_key = $1", topic_key)
            if topic_id is None:
                topic_id = await con.fetchval(
                    "INSERT INTO topics (canonical_name, topic_key) VALUES ($1, $2) "
                    "ON CONFLICT (topic_key) DO NOTHING RETURNING id",
                    surface_form, topic_key,
                )
                if topic_id is None:  # lost a race — someone else created it
                    topic_id = await con.fetchval(
                        "SELECT id FROM topics WHERE topic_key = $1", topic_key
                    )
            await con.execute(
                "INSERT INTO topic_aliases (topic_id, market, surface_form) VALUES ($1, $2, $3) "
                "ON CONFLICT (market, surface_form) DO NOTHING",
                topic_id, market, surface_form,
            )
            return topic_id

        row = await con.fetchrow(
            "SELECT topic_id FROM topic_aliases WHERE market = $1 AND surface_form = $2",
            market, surface_form,
        )
        if row:
            return row["topic_id"]
        topic_id = await con.fetchval(
            "INSERT INTO topics (canonical_name) VALUES ($1) RETURNING id", surface_form
        )
        await con.execute(
            "INSERT INTO topic_aliases (topic_id, market, surface_form) VALUES ($1, $2, $3) "
            "ON CONFLICT (market, surface_form) DO NOTHING",
            topic_id, market, surface_form,
        )
        return topic_id

    async def merge_topics(self, con: asyncpg.Connection, keep: int, drop: int) -> None:
        """Fold topic `drop` into `keep` (same real-world topic found twice)."""
        if keep == drop:
            return
        await con.execute(
            """
            INSERT INTO topic_metrics (topic_id, market, date, source, metric_name, value)
            SELECT $1, market, date, source, metric_name, value
            FROM topic_metrics WHERE topic_id = $2
            ON CONFLICT (topic_id, market, source, metric_name, date) DO NOTHING
            """,
            keep, drop,
        )
        await con.execute("DELETE FROM topic_metrics WHERE topic_id = $1", drop)
        await con.execute("UPDATE topic_aliases SET topic_id = $1 WHERE topic_id = $2", keep, drop)
        await con.execute("UPDATE videos SET topic_id = $1 WHERE topic_id = $2", keep, drop)
        drop_key = await con.fetchval("SELECT topic_key FROM topics WHERE id = $1", drop)
        keep_key = await con.fetchval("SELECT topic_key FROM topics WHERE id = $1", keep)
        if drop_key and keep_key:
            await self._rekey_watchlist(con, drop_key, keep_key)
        await con.execute("DELETE FROM topics WHERE id = $1", drop)
        log.info("topics.merged", keep=keep, drop=drop)

    @staticmethod
    async def _rekey_watchlist(con: asyncpg.Connection, old: str, new: str) -> None:
        # A market may already track `new` under another title: drop the dupes.
        await con.execute(
            """
            DELETE FROM watchlist w USING watchlist k
            WHERE w.topic_key = $1 AND k.topic_key = $2
              AND w.market = k.market AND w.kind = k.kind
            """,
            old, new,
        )
        await con.execute("UPDATE watchlist SET topic_key = $2 WHERE topic_key = $1", old, new)

    async def ensure_seed_topic(
        self, seed: Seed, qid: str | None = None, wiki_langs: list[str] | None = None
    ) -> str:
        """Create/refresh a seed topic; if Wikidata already gave us this entity
        under 'wd:Q..', merge it into the seed so history isn't split."""
        async with self.pool.acquire() as con, con.transaction():
            seed_id = await con.fetchval(
                "SELECT id FROM topics WHERE topic_key = $1", seed.topic_key
            )
            if seed_id is None:
                seed_id = await con.fetchval(
                    "INSERT INTO topics (canonical_name, topic_key) VALUES ($1, $2) RETURNING id",
                    seed.name, seed.topic_key,
                )
            if qid:
                other = await con.fetchval(
                    "SELECT id FROM topics WHERE wikidata_qid = $1 AND id <> $2", qid, seed_id
                )
                if other is not None:
                    await self.merge_topics(con, keep=seed_id, drop=other)
            await con.execute(
                "UPDATE topics SET canonical_name = $2, wikidata_qid = COALESCE($3, wikidata_qid), "
                "wiki_langs = COALESCE($4, wiki_langs) WHERE id = $1",
                seed_id, seed.name, qid, wiki_langs,
            )
        return seed.topic_key

    async def ensure_topic_for_qid(
        self, qid: str, name: str, wiki_langs: list[str] | None, kind: str | None = None
    ) -> str:
        """topic_key for a Wikidata entity (reusing a seed topic that owns it)."""
        async with self.pool.acquire() as con:
            key = await con.fetchval("SELECT topic_key FROM topics WHERE wikidata_qid = $1", qid)
            if key:
                await con.execute(
                    "UPDATE topics SET wiki_langs = COALESCE($2, wiki_langs), "
                    "kind = COALESCE($3, kind) WHERE wikidata_qid = $1",
                    qid, wiki_langs, kind,
                )
                return key
            key = f"wd:{qid}"
            await con.execute(
                """
                INSERT INTO topics (canonical_name, topic_key, wikidata_qid, wiki_langs, kind)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (topic_key) DO UPDATE SET
                    wikidata_qid = EXCLUDED.wikidata_qid,
                    wiki_langs = COALESCE(EXCLUDED.wiki_langs, topics.wiki_langs)
                """,
                name, key, qid, wiki_langs, kind,
            )
            return key

    # ================================================================ signals
    async def upsert_signals(self, signals: list[RawSignal]) -> int:
        """Persist RawSignals idempotently. Returns metric rows written/updated.

        Entities (videos/channels) are upserted individually (few); metrics go
        through COPY -> staging -> one INSERT ... ON CONFLICT. Duplicate keys in
        one batch are collapsed (last wins) before the insert.
        """
        if not signals:
            return 0
        async with self.pool.acquire() as con, con.transaction():
            topic_ids: dict[tuple[str, str, str | None], int] = {}
            metrics: dict[tuple[int, str, str, str, date], float] = {}
            for sig in signals:
                tkey = (sig.market, sig.topic_surface_form, sig.topic_key)
                if tkey not in topic_ids:
                    topic_ids[tkey] = await self.resolve_topic(
                        con, sig.market, sig.topic_surface_form, sig.topic_key
                    )
                topic_id = topic_ids[tkey]
                if sig.channel and sig.channel.get("id"):
                    await self._upsert_channel(con, sig.channel)
                if sig.video and sig.video.get("id"):
                    await self._upsert_video(con, sig.video, topic_id)
                if sig.metric_name:  # empty = entity-only signal
                    metrics[(topic_id, sig.market, sig.source, sig.metric_name, sig.observed_at)] = (
                        float(sig.value)
                    )
            if not metrics:
                return 0
            await con.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _tm_stage ("
                " topic_id BIGINT, market TEXT, date DATE, source TEXT,"
                " metric_name TEXT, value DOUBLE PRECISION) ON COMMIT DELETE ROWS"
            )
            await con.copy_records_to_table(
                "_tm_stage",
                records=[(t, m, d, s, n, v) for (t, m, s, n, d), v in metrics.items()],
                columns=_METRIC_COLS,
            )
            await con.execute(
                """
                INSERT INTO topic_metrics (topic_id, market, date, source, metric_name, value)
                SELECT topic_id, market, date, source, metric_name, value FROM _tm_stage
                ON CONFLICT (topic_id, market, source, metric_name, date)
                DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                WHERE topic_metrics.value IS DISTINCT FROM EXCLUDED.value
                """
            )
            return len(metrics)

    async def _upsert_channel(self, con: asyncpg.Connection, ch: dict[str, Any]) -> None:
        await con.execute(
            """
            INSERT INTO channels (id, title, subs, total_views, country, created_at, fetched_at)
            VALUES ($1, $2, $3, $4, $5, $6, now())
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title, subs = EXCLUDED.subs,
                total_views = EXCLUDED.total_views, country = EXCLUDED.country,
                created_at = COALESCE(EXCLUDED.created_at, channels.created_at),
                fetched_at = now()
            """,
            ch["id"], ch.get("title"), ch.get("subs"), ch.get("total_views"),
            ch.get("country"), ch.get("created_at"),
        )

    async def _upsert_video(self, con: asyncpg.Connection, v: dict[str, Any], topic_id: int) -> None:
        await con.execute(
            """
            INSERT INTO videos
                (id, channel_id, topic_id, title, views, published_at, duration, market, fetched_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
            ON CONFLICT (id) DO UPDATE SET
                views = EXCLUDED.views, title = EXCLUDED.title,
                topic_id = EXCLUDED.topic_id, fetched_at = now()
            """,
            v["id"], v.get("channel_id"), topic_id, v.get("title"), v.get("views"),
            v.get("published_at"), v.get("duration"), v.get("market"),
        )

    async def last_dates(self, market: str, source: str, metric: str) -> dict[str, date]:
        rows = await self.pool.fetch(
            """
            SELECT t.topic_key, max(tm.date) AS last
            FROM topic_metrics tm JOIN topics t ON t.id = tm.topic_id
            WHERE tm.market = $1 AND tm.source = $2 AND tm.metric_name = $3
              AND t.topic_key IS NOT NULL
            GROUP BY t.topic_key
            """,
            market, source, metric,
        )
        return {r["topic_key"]: r["last"] for r in rows}

    # ================================================================ watchlist
    async def watchlist_active(self, market: str, kind: str) -> list[WatchItem]:
        rows = await self.pool.fetch(
            """
            SELECT term, topic_key, origin FROM watchlist
            WHERE market = $1 AND kind = $2 AND active AND topic_key IS NOT NULL
            ORDER BY (origin = 'seed') DESC, last_seen DESC, hits DESC, term
            """,
            market, kind,
        )
        return [WatchItem(r["term"], r["topic_key"], r["origin"]) for r in rows]

    async def watchlist_rows(self, market: str, kind: str) -> dict[str, dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT term, topic_key, origin, active, excluded_reason FROM watchlist "
            "WHERE market = $1 AND kind = $2",
            market, kind,
        )
        return {r["term"]: dict(r) for r in rows}

    async def watchlist_upsert(
        self,
        market: str,
        kind: str,
        term: str,
        topic_key: str | None,
        origin: str,
        *,
        active: bool = True,
        excluded_reason: str | None = None,
        today: date | None = None,
    ) -> bool:
        """Insert a new watch row, or refresh an existing one. Returns True if new.

        Seeds always win: an existing seed row keeps its origin/topic. A topic
        already tracked in this market under another title is not duplicated.
        Class-excluded rows stay excluded; stale/evicted rows are revived.
        """
        today = today or datetime.now(UTC).date()
        async with self.pool.acquire() as con, con.transaction():
            existing = await con.fetchrow(
                "SELECT origin, excluded_reason FROM watchlist "
                "WHERE market = $1 AND kind = $2 AND term = $3 FOR UPDATE",
                market, kind, term,
            )
            if existing is None and topic_key:
                dup = await con.fetchval(
                    "SELECT 1 FROM watchlist WHERE market = $1 AND kind = $2 "
                    "AND topic_key = $3 LIMIT 1",
                    market, kind, topic_key,
                )
                if dup:
                    return False
            if existing is None:
                await con.execute(
                    """
                    INSERT INTO watchlist (market, kind, term, topic_key, origin, first_seen,
                                           last_seen, hits, active, excluded_reason)
                    VALUES ($1, $2, $3, $4, $5, $6, $6, 1, $7, $8)
                    """,
                    market, kind, term, topic_key, origin, today, active, excluded_reason,
                )
                return True
            if origin == "seed":
                await con.execute(
                    "UPDATE watchlist SET origin = 'seed', topic_key = $4, active = TRUE, "
                    "excluded_reason = NULL, last_seen = $5, hits = hits + 1 "
                    "WHERE market = $1 AND kind = $2 AND term = $3",
                    market, kind, term, topic_key, today,
                )
            else:
                await con.execute(
                    """
                    UPDATE watchlist SET last_seen = $4, hits = hits + 1,
                        origin = CASE WHEN array_position($5::text[], $6)
                                         > array_position($5::text[], origin)
                                      THEN $6 ELSE origin END,
                        active = CASE WHEN excluded_reason IN ('stale', 'evicted_cap')
                                      THEN TRUE ELSE active END,
                        excluded_reason = CASE WHEN excluded_reason IN ('stale', 'evicted_cap')
                                               THEN NULL ELSE excluded_reason END
                    WHERE market = $1 AND kind = $2 AND term = $3
                    """,
                    market, kind, term, today, ORIGIN_PRIORITY, origin,
                )
            return False

    async def watchlist_touch_topic(self, topic_key: str, today: date) -> None:
        """Seeing a topic anywhere keeps its cross-language rows fresh too."""
        await self.pool.execute(
            """
            UPDATE watchlist SET last_seen = GREATEST(last_seen, $2),
                active = CASE WHEN excluded_reason IN ('stale', 'evicted_cap') THEN TRUE ELSE active END,
                excluded_reason = CASE WHEN excluded_reason IN ('stale', 'evicted_cap')
                                       THEN NULL ELSE excluded_reason END
            WHERE topic_key = $1 AND origin = 'langlink'
            """,
            topic_key, today,
        )

    async def watchlist_evict(
        self, market: str, kind: str, *, cap: int, stale_after_days: int, today: date
    ) -> dict[str, int]:
        """Deactivate stale rows, then everything beyond the cap. Seeds are exempt."""
        cutoff = today - timedelta(days=stale_after_days)
        stale = await self.pool.fetchval(
            """
            WITH s AS (
                UPDATE watchlist SET active = FALSE, excluded_reason = 'stale'
                WHERE market = $1 AND kind = $2 AND active AND origin <> 'seed'
                  AND last_seen < $3
                RETURNING 1)
            SELECT count(*) FROM s
            """,
            market, kind, cutoff,
        )
        capped = await self.pool.fetchval(
            """
            WITH ranked AS (
                SELECT term, row_number() OVER (
                    ORDER BY (origin = 'seed') DESC, last_seen DESC, hits DESC, term) AS rn
                FROM watchlist WHERE market = $1 AND kind = $2 AND active),
            c AS (
                UPDATE watchlist w SET active = FALSE, excluded_reason = 'evicted_cap'
                FROM ranked r
                WHERE w.market = $1 AND w.kind = $2 AND w.term = r.term
                  AND r.rn > $3 AND w.origin <> 'seed'
                RETURNING 1)
            SELECT count(*) FROM c
            """,
            market, kind, cap,
        )
        return {"stale": int(stale or 0), "evicted_cap": int(capped or 0)}

    async def watchlist_summary(self) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT market, origin, active, count(*) AS n FROM watchlist "
            "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3 DESC"
        )

    # ================================================================ runs
    async def start_run(self, collector: str, market: str | None) -> int:
        return await self.pool.fetchval(
            "INSERT INTO collector_runs (collector, market, status) VALUES ($1, $2, 'running') "
            "RETURNING id",
            collector, market,
        )

    async def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        items: int = 0,
        errors: int = 0,
        quota_used: int = 0,
        detail: dict[str, Any] | None = None,
    ) -> None:
        await self.pool.execute(
            "UPDATE collector_runs SET finished_at = now(), status = $2, items = $3, "
            "errors = $4, quota_used = $5, detail = $6 WHERE id = $1",
            run_id, status, items, errors, quota_used,
            json.dumps(detail, default=str) if detail is not None else None,
        )

    async def close_orphan_runs(self) -> int:
        """Runs left 'running' by a crashed process are marked as errors."""
        n = await self.pool.fetchval(
            """
            WITH o AS (
                UPDATE collector_runs SET status = 'error', finished_at = now(),
                    detail = '{"error": "process died before finishing"}'::jsonb
                WHERE status = 'running' RETURNING 1)
            SELECT count(*) FROM o
            """
        )
        return int(n or 0)


class DbState:
    """CollectorState backed by the database."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def watchlist(self, market: str, kind: str) -> list[WatchItem]:
        return await self.db.watchlist_active(market, kind)

    async def last_dates(self, market: str, source: str, metric: str) -> dict[str, date]:
        return await self.db.last_dates(market, source, metric)

    async def observed_dates(self, market: str, source: str, metric: str
                             ) -> dict[str, set[date]]:
        rows = await self.db.pool.fetch(
            """
            SELECT t.topic_key, array_agg(tm.date) AS dates
            FROM topic_metrics tm JOIN topics t ON t.id = tm.topic_id
            WHERE tm.market = $1 AND tm.source = $2 AND tm.metric_name = $3
              AND t.topic_key IS NOT NULL
            GROUP BY t.topic_key
            """,
            market, source, metric,
        )
        return {r["topic_key"]: set(r["dates"]) for r in rows}

    async def pending_probes(self, market: str, limit: int) -> list[Probe]:
        rows = await self.db.pool.fetch(
            "SELECT topic_key, query FROM probe_queue WHERE market = $1 AND probed_at IS NULL "
            "ORDER BY score DESC NULLS LAST, queued_at LIMIT $2",
            market, limit,
        )
        return [Probe(r["topic_key"], r["query"]) for r in rows]

    async def probed(self, market: str) -> list[Probe]:
        rows = await self.db.pool.fetch(
            "SELECT topic_key, query FROM probe_queue WHERE market = $1 "
            "AND probed_at IS NOT NULL ORDER BY score DESC NULLS LAST",
            market,
        )
        return [Probe(r["topic_key"], r["query"]) for r in rows]

    async def top_topics(self, market: str, limit: int) -> list[Probe]:
        rows = await self.db.pool.fetch(
            """
            SELECT t.topic_key, s.details->>'name' AS name
            FROM scores s JOIN topics t ON t.id = s.topic_id
            WHERE s.market = $1 AND t.topic_key IS NOT NULL
              AND s.date = (SELECT max(date) FROM scores WHERE market = $1)
              AND s.phase <> 'UNKNOWN'
            ORDER BY s.composite DESC LIMIT $2
            """,
            market, limit,
        )
        return [Probe(r["topic_key"], r["name"]) for r in rows if r["name"]]

    async def mark_probed(self, market: str, topic_key: str) -> None:
        await self.db.pool.execute(
            "UPDATE probe_queue SET probed_at = now() WHERE market = $1 AND topic_key = $2",
            market, topic_key,
        )


class PgQuotaStore:
    """QuotaStore backed by the api_quota table (persistent across runs)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get_used(self, api: str, day: date) -> int:
        val = await self.db.pool.fetchval(
            "SELECT units_used FROM api_quota WHERE api = $1 AND date = $2", api, day
        )
        return int(val or 0)

    async def add_used(self, api: str, day: date, units: int, daily_limit: int) -> None:
        await self.db.pool.execute(
            """
            INSERT INTO api_quota (api, date, units_used, daily_limit)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (api, date)
            DO UPDATE SET units_used = api_quota.units_used + EXCLUDED.units_used,
                          daily_limit = EXCLUDED.daily_limit, updated_at = now()
            """,
            api, day, units, daily_limit,
        )
