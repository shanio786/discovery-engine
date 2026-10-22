"""Command-line entrypoint.

    python -m ode.cli initdb                 # apply schema (+ hypertable)
    python -m ode.cli collect                # run all collectors x all markets
    python -m ode.cli collect --source wikipedia --market en-US
    python -m ode.cli status                 # recent collector_runs summary
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ode.config import get_secrets, load_config
from ode.db import Database
from ode.logging import configure, get_logger

log = get_logger("ode.cli")


async def _initdb() -> None:
    secrets = get_secrets()
    db = await Database.connect(secrets.database_url)
    try:
        await db.init_schema()
        log.info("initdb.done")
    finally:
        await db.close()


async def _collect(sources: list[str] | None, markets: list[str] | None) -> int:
    from ode.pipeline import harvest

    config = load_config()
    secrets = get_secrets()
    results = await harvest(config, secrets, sources=sources, markets=markets)
    ok = sum(1 for r in results if r["status"] == "ok")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    errored = sum(1 for r in results if r["status"] == "error")
    items = sum(r.get("items", 0) for r in results)
    log.info("collect.summary", ok=ok, skipped=skipped, errored=errored, items=items)
    for r in results:
        print(
            f"  {r['status']:8} {r['collector']:11} {r.get('market',''):6} "
            f"items={r.get('items','-')} {r.get('error','')}"
        )
    return 1 if errored else 0


async def _status(limit: int) -> None:
    secrets = get_secrets()
    db = await Database.connect(secrets.database_url)
    try:
        rows = await db.pool.fetch(
            "SELECT collector, market, status, items, errors, quota_used, "
            "started_at, finished_at FROM collector_runs "
            "ORDER BY started_at DESC LIMIT $1",
            limit,
        )
        if not rows:
            print("no collector runs yet")
            return
        for r in rows:
            print(
                f"  {r['started_at']:%Y-%m-%d %H:%M} {r['status']:8} "
                f"{r['collector']:11} {r['market'] or '':6} "
                f"items={r['items']} errors={r['errors']} quota={r['quota_used']}"
            )
    finally:
        await db.close()


def main(argv: list[str] | None = None) -> int:
    configure()
    parser = argparse.ArgumentParser(prog="ode", description="Opportunity Discovery Engine")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("initdb", help="apply SQL schema and hypertable")

    c = sub.add_parser("collect", help="run collectors")
    c.add_argument("--source", action="append", help="limit to source(s); repeatable")
    c.add_argument("--market", action="append", help="limit to market(s); repeatable")

    s = sub.add_parser("status", help="show recent collector runs")
    s.add_argument("--limit", type=int, default=20)

    args = parser.parse_args(argv)

    if args.cmd == "initdb":
        asyncio.run(_initdb())
        return 0
    if args.cmd == "collect":
        return asyncio.run(_collect(args.source, args.market))
    if args.cmd == "status":
        asyncio.run(_status(args.limit))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
