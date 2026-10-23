"""Your work log in the database: what you started / published / dropped and
what it earned. Pure maths lives in `ode.learning`."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date
from typing import Any

from ode.config import Config
from ode.db import Database
from ode.learning import WorkRow, calibrate_market_values, calibrate_platforms

STATUSES = ("started", "published", "dropped")


class UnknownAction(LookupError):
    """No plan item with that id was ever shown."""


async def save_plan_items(db: Database, day: date, plan: Iterable[Any]) -> int:
    """Snapshot shown plan items so `ode done <id>` always resolves later."""
    rows = [(a.action_id, day, a.topic_id, a.topic, a.market, a.platform, a.score,
             json.dumps({"headline": a.headline, "ease": a.ease, "value": a.value,
                         "language": a.language}, ensure_ascii=False))
            for a in plan]
    if rows:
        await db.pool.executemany(
            """
            INSERT INTO plan_items (action_id, day, topic_id, topic, market, platform, score,
                                    payload)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
            ON CONFLICT (action_id) DO UPDATE SET day = EXCLUDED.day, score = EXCLUDED.score,
                payload = EXCLUDED.payload, topic = EXCLUDED.topic
            """, rows)
    return len(rows)


async def logged_ids(db: Database) -> set[str]:
    """Every action you already touched (any status): never suggested again."""
    return {r["action_id"] for r in await db.pool.fetch("SELECT action_id FROM work_log")}


async def mark(db: Database, action_id: str, status: str = "started", *,
               hours: float | None = None, note: str | None = None) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    item = await db.pool.fetchrow("SELECT * FROM plan_items WHERE action_id = $1", action_id)
    if item is None:
        raise UnknownAction(f"no plan item {action_id!r} — copy the id from `ode plan`")
    row = await db.pool.fetchrow(
        """
        INSERT INTO work_log (action_id, topic_id, topic, market, platform, status, hours, note)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (action_id) DO UPDATE SET status = EXCLUDED.status, updated_at = now(),
            hours = COALESCE(EXCLUDED.hours, work_log.hours),
            note = COALESCE(EXCLUDED.note, work_log.note)
        RETURNING *
        """,
        action_id, item["topic_id"], item["topic"], item["market"], item["platform"],
        status, hours, note)
    return dict(row)


async def record_result(db: Database, action_id: str, *, views: int | None = None,
                        revenue: float | None = None, hours: float | None = None
                        ) -> dict[str, Any]:
    """Cumulative totals so far (overwrite, not add): enter what the dashboards show."""
    if await db.pool.fetchval("SELECT 1 FROM work_log WHERE action_id = $1", action_id) is None:
        await mark(db, action_id, "published")
    row = await db.pool.fetchrow(
        """
        UPDATE work_log SET views = COALESCE($2, views), revenue_usd = COALESCE($3, revenue_usd),
            hours = COALESCE($4, hours), updated_at = now(),
            status = CASE WHEN status = 'started' THEN 'published' ELSE status END
        WHERE action_id = $1 RETURNING *
        """, action_id, views, revenue, hours)
    return dict(row)


async def entries(db: Database, limit: int = 100) -> list[dict[str, Any]]:
    rows = await db.pool.fetch(
        "SELECT * FROM work_log ORDER BY updated_at DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


async def calibration(db: Database, config: Config) -> tuple[dict[str, float], dict[str, float]]:
    """(market values, platform multipliers) learned from your results.

    Off with `learning.enabled: false`; with no results both are the priors.
    """
    priors = {m: config.market_value(m) for m in config.market_codes}
    cfg = config.learning
    if not cfg.get("enabled", True):
        return priors, {}
    rows = [WorkRow(r["market"], r["platform"], r["views"], r["revenue_usd"], r["hours"])
            for r in await db.pool.fetch(
                "SELECT market, platform, views, revenue_usd, hours FROM work_log "
                "WHERE status = 'published' AND revenue_usd > 0")]
    return (calibrate_market_values(rows, priors,
                                    pseudo_views=float(cfg.get("pseudo_views", 100_000))),
            calibrate_platforms(rows, pseudo_items=float(cfg.get("pseudo_items", 3))))
