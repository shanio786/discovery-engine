"""Harvest orchestration.

`daily()` is the one entry point cron needs: apply schema -> discover ->
collect -> score -> miners -> briefs, all under a Postgres advisory lock so overlapping jobs can't race.
Every collector x market attempt is recorded in `collector_runs` as
ok | partial | skipped | error, with item, error and quota counts. A failure
in one collector or market never aborts the others.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from ode.collectors import COLLECTOR_CLASSES
from ode.collectors.base import BaseCollector
from ode.config import Config, Secrets
from ode.db import Database, DbState, PgQuotaStore
from ode.logging import get_logger
from ode.models import Market
from ode.quota import QuotaTracker
from ode.state import CollectorState

log = get_logger(__name__)

USER_AGENT = "ode-discovery-engine/0.2 (+https://github.com/shanio786/discovery-engine)"


def http_client(config: Config) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=config.http_timeout,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
    )


async def run_one(db: Database, collector: BaseCollector, market: Market, config: Config) -> dict:
    """Run a single collector for a single market. Returns a summary dict."""
    run_id = await db.start_run(collector.name, market.code)
    lg = log.bind(collector=collector.name, market=market.code, run_id=run_id)
    base = {"collector": collector.name, "market": market.code}

    for check in (collector.is_enabled, lambda: collector.supports(market)):
        ok, reason = check()
        if not ok:
            lg.info("collector.skipped", reason=reason)
            await db.finish_run(run_id, status="skipped", detail={"reason": reason})
            return {**base, "status": "skipped", "reason": reason}

    since = datetime.now(UTC) - timedelta(days=config.lookback_days(collector.name))
    collector.reset_run_stats()
    try:
        signals = await collector.collect(market, since)
        written = await db.upsert_signals(signals)
    except Exception as exc:  # noqa: BLE001 - isolate failures per collector/market
        lg.exception("collector.error", error=str(exc)[:300])
        await db.finish_run(run_id, status="error", errors=collector.errors + 1,
                            quota_used=collector.last_quota_used,
                            detail={"error": str(exc)[:500]})
        return {**base, "status": "error", "error": str(exc)[:200]}

    if collector.skip_reason and not signals:
        lg.info("collector.skipped", reason=collector.skip_reason)
        await db.finish_run(run_id, status="skipped", detail={"reason": collector.skip_reason})
        return {**base, "status": "skipped", "reason": collector.skip_reason}
    if (collector.aborted or collector.errors) and not written:
        status = "error"  # tried, failed, saved nothing: never report that as partial/ok
    elif collector.partial:
        status = "partial"
    else:
        status = "ok"
    detail = {k: v for k, v in {"aborted": collector.aborted, "notes": collector.notes}.items() if v}
    await db.finish_run(run_id, status=status, items=written, errors=collector.errors,
                        quota_used=collector.last_quota_used, detail=detail or None)
    lg.info("collector.done", status=status, items=written, errors=collector.errors,
            quota_used=collector.last_quota_used)
    return {**base, "status": status, "items": written, "errors": collector.errors,
            **({"error": collector.aborted} if collector.aborted else {})}


def build_collectors(
    config: Config,
    secrets: Secrets,
    client: httpx.AsyncClient,
    quota: QuotaTracker,
    state: CollectorState | None = None,
    only: list[str] | None = None,
) -> list[BaseCollector]:
    if only:
        known = {c.name for c in COLLECTOR_CLASSES}
        unknown = set(only) - known
        if unknown:
            raise ValueError(f"unknown source(s) {sorted(unknown)}; known: {sorted(known)}")
    return [
        cls(config, secrets, client, quota, state)
        for cls in COLLECTOR_CLASSES
        if not only or cls.name in only
    ]


async def _collect(
    db: Database, config: Config, secrets: Secrets, client: httpx.AsyncClient,
    sources: list[str] | None, markets: list[str] | None,
) -> list[dict]:
    quota = QuotaTracker(PgQuotaStore(db), config.quotas)
    market_objs = [config.market(m) for m in markets] if markets else config.markets()
    collectors = build_collectors(config, secrets, client, quota, DbState(db), only=sources)
    results: list[dict] = []
    for collector in collectors:
        for market in market_objs:
            results.append(await run_one(db, collector, market, config))
    return results


async def harvest(
    config: Config, secrets: Secrets, *,
    sources: list[str] | None = None, markets: list[str] | None = None,
) -> list[dict]:
    """Collect only (no discovery). Locked against concurrent runs."""
    db = await Database.connect(secrets.database_url)
    try:
        async with db.harvest_lock():
            await db.close_orphan_runs()
            async with http_client(config) as client:
                return await _collect(db, config, secrets, client, sources, markets)
    finally:
        await db.close()


async def discover(config: Config, secrets: Secrets, *, markets: list[str] | None = None) -> list[dict]:
    from ode.discovery import Discoverer

    db = await Database.connect(secrets.database_url)
    try:
        async with db.harvest_lock(), http_client(config) as client:
            return await Discoverer(config, client, db).run(markets)
    finally:
        await db.close()


async def score(config: Config, secrets: Secrets, *, as_of=None,
                markets: list[str] | None = None) -> list[dict]:
    from ode.scoring.scorer import Scorer

    db = await Database.connect(secrets.database_url)
    try:
        async with db.harvest_lock():
            return await Scorer(config, db).run(as_of or datetime.now(UTC).date(), markets)
    finally:
        await db.close()


async def _briefs(db: Database, config: Config, secrets: Secrets, today) -> dict:
    """Gemini briefs for today's plan; never fails the daily run."""
    from ode.briefs import generate_briefs
    from ode.scoring.plan import build_plan

    try:
        plan = await build_plan(db, config, today)
        async with http_client(config) as client:
            return await generate_briefs(db, config, secrets, plan, client)
    except Exception as exc:  # noqa: BLE001 - briefs are optional extras
        log.warning("briefs.step_failed", error=str(exc)[:300])
        return {"status": "error", "error": str(exc)[:200]}


async def _miners(db: Database, config: Config, secrets: Secrets) -> dict:
    """Questions / complaints / formats for today's plan; never fails the daily run."""
    from ode.miners import run_miners

    try:
        async with http_client(config) as client:
            return await run_miners(db, config, secrets, client)
    except Exception as exc:  # noqa: BLE001 - research is an extra
        log.warning("miners.step_failed", error=str(exc)[:300])
        return {"status": "error", "error": str(exc)[:200]}


async def mine(config: Config, secrets: Secrets) -> dict:
    from ode.miners import run_miners

    db = await Database.connect(secrets.database_url)
    try:
        async with db.harvest_lock(), http_client(config) as client:
            return await run_miners(db, config, secrets, client)
    finally:
        await db.close()


async def daily(config: Config, secrets: Secrets) -> dict:
    """Schema -> discovery -> collection -> scoring -> briefs, under one lock (cron)."""
    from ode.discovery import Discoverer
    from ode.scoring.scorer import Scorer

    db = await Database.connect(secrets.database_url)
    try:
        async with db.harvest_lock():
            await db.init_schema()
            await db.close_orphan_runs()
            async with http_client(config) as client:
                disc = await Discoverer(config, client, db).run()
                coll = await _collect(db, config, secrets, client, None, None)
            today = datetime.now(UTC).date()
            scored = await Scorer(config, db).run(today)
            mined = await _miners(db, config, secrets)
            briefs = await _briefs(db, config, secrets, today)
        return {"discovery": disc, "collect": coll, "scoring": scored, "miners": mined,
                "briefs": briefs}
    finally:
        await db.close()
