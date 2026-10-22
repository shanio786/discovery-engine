"""Harvest orchestration.

Runs each enabled collector across each market, persists signals idempotently,
and records every attempt in `collector_runs` (ok | skipped | error) with item
and quota counts. A crash in one collector/market never aborts the others.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from ode.collectors import COLLECTOR_CLASSES
from ode.collectors.base import BaseCollector
from ode.config import Config, Secrets
from ode.db import Database, PgQuotaStore
from ode.logging import get_logger
from ode.models import Market
from ode.quota import QuotaTracker

log = get_logger(__name__)

USER_AGENT = "ode-discovery-engine/0.1 (+https://github.com/shanio786/discovery-engine)"


async def run_one(db: Database, collector: BaseCollector, market: Market, config: Config) -> dict:
    """Run a single collector for a single market. Returns a summary dict."""
    run_id = await db.start_run(collector.name, market.code)
    l = log.bind(collector=collector.name, market=market.code, run_id=run_id)

    enabled, reason = collector.is_enabled()
    if not enabled:
        l.info("collector.skipped", reason=reason)
        await db.finish_run(run_id, status="skipped", detail={"reason": reason})
        return {"collector": collector.name, "market": market.code, "status": "skipped"}

    since = datetime.now(UTC) - timedelta(days=config.lookback_days(collector.name))
    collector.last_quota_used = 0
    try:
        signals = await collector.collect(market, since)
        written = await db.upsert_signals(signals)
        await db.finish_run(
            run_id, status="ok", items=written, quota_used=collector.last_quota_used
        )
        l.info("collector.ok", items=written, quota_used=collector.last_quota_used)
        return {"collector": collector.name, "market": market.code, "status": "ok",
                "items": written}
    except Exception as exc:  # noqa: BLE001 - isolate failures per collector/market
        l.error("collector.error", error=str(exc), exc_info=True)
        await db.finish_run(
            run_id, status="error", errors=1,
            quota_used=collector.last_quota_used, detail={"error": str(exc)},
        )
        return {"collector": collector.name, "market": market.code, "status": "error",
                "error": str(exc)}


def build_collectors(
    config: Config, secrets: Secrets, client: httpx.AsyncClient, quota: QuotaTracker,
    only: list[str] | None = None,
) -> list[BaseCollector]:
    collectors = []
    for cls in COLLECTOR_CLASSES:
        if only and cls.name not in only:
            continue
        collectors.append(cls(config, secrets, client, quota))
    return collectors


async def harvest(
    config: Config, secrets: Secrets, *,
    sources: list[str] | None = None, markets: list[str] | None = None,
) -> list[dict]:
    """Top-level entry: open DB + HTTP, run the matrix, return summaries."""
    db = await Database.connect(secrets.database_url)
    results: list[dict] = []
    try:
        quota = QuotaTracker(PgQuotaStore(db), config.quotas)
        market_objs = (
            [config.market(m) for m in markets] if markets else config.markets()
        )
        async with httpx.AsyncClient(
            timeout=config.http_timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        ) as client:
            collectors = build_collectors(config, secrets, client, quota, only=sources)
            for collector in collectors:
                for market in market_objs:
                    results.append(await run_one(db, collector, market, config))
    finally:
        await db.close()
    return results
