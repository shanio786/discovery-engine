"""Postgres/TimescaleDB access layer (asyncpg).

All writes are idempotent (ON CONFLICT upserts) so crashed/re-run collectors
never duplicate or lose data — a Phase 1 engineering requirement.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import asyncpg

from ode.logging import get_logger
from ode.models import RawSignal

log = get_logger(__name__)

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


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

    # ---------- schema ----------
    async def init_schema(self) -> None:
        """Apply sql/*.sql in order. Hypertable step is best-effort: if the
        timescaledb extension is unavailable we keep the plain table so the
        rest of Phase 1 still runs (and we log a clear warning)."""
        schema = (SQL_DIR / "001_schema.sql").read_text(encoding="utf-8")
        async with self.pool.acquire() as con:
            await con.execute(schema)
        hyper = (SQL_DIR / "002_hypertables.sql").read_text(encoding="utf-8")
        try:
            async with self.pool.acquire() as con:
                await con.execute(hyper)
            log.info("schema.ready", hypertable=True)
        except asyncpg.PostgresError as exc:
            log.warning(
                "schema.hypertable_skipped",
                reason=str(exc),
                hint="install TimescaleDB for time-series performance",
            )

    # ---------- topic resolution ----------
    async def resolve_topic(self, con: asyncpg.Connection, market: str, surface_form: str) -> int:
        """Get-or-create a topic for (market, surface_form). Returns topic_id.

        Phase 1 keeps resolution simple: one surface form per market maps to one
        topic. Cross-language/semantic merging arrives in Phase 5 (clustering).
        """
        row = await con.fetchrow(
            "SELECT topic_id FROM topic_aliases WHERE market = $1 AND surface_form = $2",
            market,
            surface_form,
        )
        if row:
            return row["topic_id"]
        topic_id = await con.fetchval(
            "INSERT INTO topics (canonical_name) VALUES ($1) RETURNING id",
            surface_form,
        )
        await con.execute(
            "INSERT INTO topic_aliases (topic_id, market, surface_form) VALUES ($1, $2, $3) "
            "ON CONFLICT (market, surface_form) DO NOTHING",
            topic_id,
            market,
            surface_form,
        )
        return topic_id

    # ---------- metric upsert ----------
    async def upsert_signals(self, signals: list[RawSignal]) -> int:
        """Persist a batch of RawSignals idempotently. Returns rows written."""
        if not signals:
            return 0
        written = 0
        async with self.pool.acquire() as con, con.transaction():
            for sig in signals:
                topic_id = await self.resolve_topic(con, sig.market, sig.topic_surface_form)
                if sig.channel:
                    await self._upsert_channel(con, sig.channel)
                if sig.video:
                    await self._upsert_video(con, sig.video, topic_id)
                # Entity-only signals (empty metric_name) carry a video/channel
                # to store but produce no time-series row.
                if not sig.metric_name:
                    continue
                await con.execute(
                    """
                        INSERT INTO topic_metrics
                            (topic_id, market, date, source, metric_name, value)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (topic_id, market, source, metric_name, date)
                        DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                        """,
                    topic_id,
                    sig.market,
                    sig.observed_at,
                    sig.source,
                    sig.metric_name,
                    float(sig.value),
                )
                written += 1
        return written

    async def _upsert_channel(self, con: asyncpg.Connection, ch: dict[str, Any]) -> None:
        await con.execute(
            """
            INSERT INTO channels (id, title, subs, total_views, country, created_at, fetched_at)
            VALUES ($1, $2, $3, $4, $5, $6, now())
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title, subs = EXCLUDED.subs,
                total_views = EXCLUDED.total_views, country = EXCLUDED.country,
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

    # ---------- collector_runs bookkeeping ----------
    async def start_run(self, collector: str, market: str | None) -> int:
        return await self.pool.fetchval(
            "INSERT INTO collector_runs (collector, market, status) VALUES ($1, $2, 'running') "
            "RETURNING id",
            collector,
            market,
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
        import json

        await self.pool.execute(
            "UPDATE collector_runs SET finished_at = now(), status = $2, items = $3, "
            "errors = $4, quota_used = $5, detail = $6 WHERE id = $1",
            run_id,
            status,
            items,
            errors,
            quota_used,
            json.dumps(detail) if detail is not None else None,
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
