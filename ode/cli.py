"""Command-line entrypoint.

    ode doctor                     # check config, DB and every API (run this first)
    ode initdb                     # apply schema (idempotent)
    ode daily                      # schema -> discover -> collect  (what cron runs)
    ode discover [--market de-DE]  # refresh watchlists only
    ode collect [--source wikipedia] [--market en-US]
    ode status [--limit 30]        # recent runs
    ode watchlist                  # what is being tracked, per market

Exit codes: 0 ok, 1 some run errored / required check failed,
2 bad config, 3 another harvest is already running.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from ode.config import ConfigError, get_secrets, load_config
from ode.db import AlreadyRunning, Database
from ode.logging import configure, get_logger

log = get_logger("ode.cli")


def _print_results(results: list[dict]) -> int:
    for r in results:
        extra = r.get("error") or r.get("reason") or ""
        print(f"  {r['status']:8} {r['collector']:11} {r.get('market', ''):6} "
              f"items={r.get('items', '-')} errors={r.get('errors', 0)} {extra}")
    return 1 if any(r["status"] == "error" for r in results) else 0


def _print_discovery(reports: list[dict]) -> int:
    for r in reports:
        print(f"  {r['status']:8} discovery   {r['market']:6} candidates={r['candidates']} "
              f"new={r['new']} excluded={r['excluded']} langlinks={r['langlinks']} "
              f"evicted={r['evicted']} days={r['days_scanned']} "
              f"{'; '.join(r['errors'])}")
    return 1 if any(r["status"] == "error" for r in reports) else 0


async def _initdb() -> int:
    db = await Database.connect(get_secrets().database_url)
    try:
        for name, state in (await db.init_schema()).items():
            print(f"  {state:8} {name}")
        return 0
    finally:
        await db.close()


async def _collect(sources: list[str] | None, markets: list[str] | None) -> int:
    from ode.pipeline import harvest

    return _print_results(await harvest(load_config(), get_secrets(), sources=sources,
                                        markets=markets))


async def _discover(markets: list[str] | None) -> int:
    from ode.pipeline import discover

    return _print_discovery(await discover(load_config(), get_secrets(), markets=markets))


async def _daily() -> int:
    from ode.pipeline import daily

    out = await daily(load_config(), get_secrets())
    print("discovery:")
    rc = _print_discovery(out["discovery"])
    print("collect:")
    return max(rc, _print_results(out["collect"]))


async def _status(limit: int) -> int:
    db = await Database.connect(get_secrets().database_url)
    try:
        rows = await db.pool.fetch(
            "SELECT collector, market, status, items, errors, quota_used, started_at, "
            "finished_at, detail FROM collector_runs ORDER BY started_at DESC LIMIT $1",
            limit,
        )
        if not rows:
            print("no collector runs yet")
            return 0
        for r in rows:
            took = (r["finished_at"] - r["started_at"]).total_seconds() if r["finished_at"] else 0
            print(f"  {r['started_at']:%Y-%m-%d %H:%M} {r['status']:8} {r['collector']:11} "
                  f"{r['market'] or '':6} items={r['items']} errors={r['errors']} "
                  f"quota={r['quota_used']} {took:.0f}s")
        metrics = await db.pool.fetch(
            "SELECT source, market, count(*) AS rows, count(DISTINCT topic_id) AS topics, "
            "max(date) AS latest FROM topic_metrics GROUP BY 1, 2 ORDER BY 1, 2"
        )
        if metrics:
            print("\n  data:")
            for m in metrics:
                print(f"    {m['source']:11} {m['market']:6} rows={m['rows']} "
                      f"topics={m['topics']} latest={m['latest']}")
        return 0
    finally:
        await db.close()


async def _watchlist() -> int:
    db = await Database.connect(get_secrets().database_url)
    try:
        rows = await db.watchlist_summary()
        if not rows:
            print("watchlist empty — run `ode discover`")
        for r in rows:
            print(f"  {r['market']:6} {r['origin']:9} {'active' if r['active'] else 'inactive':8} "
                  f"{r['n']}")
        return 0
    finally:
        await db.close()


async def _doctor() -> int:
    from ode.doctor import run_doctor

    checks = await run_doctor(load_config(), get_secrets())
    for c in checks:
        mark = "OK  " if c.ok else ("FAIL" if c.required else "warn")
        print(f"  [{mark}] {c.name:20} {c.detail}")
    failed = [c for c in checks if c.required and not c.ok]
    print(f"\n  {'ALL REQUIRED CHECKS PASSED' if not failed else f'{len(failed)} REQUIRED CHECK(S) FAILED'}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    configure(level=os.environ.get("ODE_LOG_LEVEL", "INFO"),
              json_logs=os.environ.get("ODE_LOG_JSON", "") not in ("", "0", "false"))
    parser = argparse.ArgumentParser(prog="ode", description="Opportunity Discovery Engine")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="check config, database and every API")
    sub.add_parser("initdb", help="apply SQL schema (idempotent)")
    sub.add_parser("daily", help="schema -> discover -> collect (cron entry point)")
    d = sub.add_parser("discover", help="refresh watchlists")
    d.add_argument("--market", action="append")
    c = sub.add_parser("collect", help="run collectors")
    c.add_argument("--source", action="append", help="limit to source(s); repeatable")
    c.add_argument("--market", action="append", help="limit to market(s); repeatable")
    s = sub.add_parser("status", help="recent runs + data summary")
    s.add_argument("--limit", type=int, default=30)
    sub.add_parser("watchlist", help="what is tracked, per market")
    args = parser.parse_args(argv)

    commands = {
        "doctor": _doctor,
        "initdb": _initdb,
        "daily": _daily,
        "discover": lambda: _discover(args.market),
        "collect": lambda: _collect(args.source, args.market),
        "status": lambda: _status(args.limit),
        "watchlist": _watchlist,
    }
    try:
        return asyncio.run(commands[args.cmd]())
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except AlreadyRunning as exc:
        print(f"skipped: {exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
