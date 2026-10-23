"""Command-line entrypoint.

    ode doctor                     # check config, DB and every API (run this first)
    ode initdb                     # apply schema (idempotent)
    ode daily                      # schema -> discover -> collect -> score (cron)
    ode score [--as-of 2026-01-31] # (re)score from stored data
    ode gems [--market de-DE] [--format md|csv|table] [--limit 30]
    ode plan [--format md|table|json]   # what to do NOW: platform + language + steps
    ode briefs                     # Gemini titles/outline for today's top actions
    ode competitors [--refresh]    # big channels / upload surges / new Google leaders on your topics
    ode niches [--refresh]         # related topics grouped: one channel / site per niche
    ode langpacks                  # Gemini pack per extra language (+ real Google searches)
    ode seo                        # DataForSEO: real Google volume / difficulty (top 20, budget)
    ode mine                       # long-tail questions, 1-star reviews, viral formats now
    ode backtest                   # did past top-20s really grow? learns composite weights
    ode export [--send]            # production pack JSON (stdout) / POST to N8N_WEBHOOK_URL
    ode web [--port 8080]          # phone dashboard (DASHBOARD_PASSWORD)
    ode scheduler                  # ALWAYS-ON: runs every job on its schedule, forever
    ode realtime hourly|trends|breakout|plan|social   # run one realtime job now
    ode done <id> [--status started|published|dropped] [--hours 3] [--note ...]
    ode result <id> --views 12000 --revenue 14.5   # totals so far: the engine learns
    ode track <url> [--id <plan id>] [--platform BLOG] [--keyword "..."]   # your published item
    ode content [--refresh]        # your items: views, verdict (WINNER / DROP ...), next 5 ideas
    ode untrack <content id>
    ode week [--hours 25] [--save] # this week's best mix of work for your hours
    ode worklog                    # what you did, what it earned, what was learned
    ode alerts [--limit 20]        # what the engine alerted (or would have)
    ode notify-test                # send a Telegram test message
    ode notify "text"              # send any message (used by scripts/backup.sh on failure)
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
from datetime import UTC, date, datetime

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


def _print_scoring(reports: list[dict]) -> int:
    for r in reports:
        if r["status"] != "ok":
            print(f"  {r['status']:8} scoring     {r['market']:6} {r.get('error', '')}")
            continue
        phases = ", ".join(f"{k}={v}" for k, v in sorted(r["phases"].items()))
        print(f"  ok       scoring     {r['market']:6} scored={r['scored']} "
              f"probes_queued={r['probes_queued']} [{phases}]")
    return 1 if any(r["status"] != "ok" for r in reports) else 0


async def _daily() -> int:
    from ode.pipeline import daily

    out = await daily(load_config(), get_secrets())
    print("discovery:")
    rc = _print_discovery(out["discovery"])
    print("collect:")
    rc = max(rc, _print_results(out["collect"]))
    print("scoring:")
    rc = max(rc, _print_scoring(out["scoring"]))
    for step in ("niches", "dataforseo", "langpacks"):
        r = out.get(step) or {}
        print(f"{step}:\n  " + "  ".join(f"{k}={v}" for k, v in r.items()))
    print("miners:")
    for name, r in (out.get("miners") or {}).items():
        if isinstance(r, dict):
            print(f"  {r.get('status', '-'):8} {name:12} " + " ".join(
                f"{k}={v}" for k, v in r.items() if k != "status"))
    b = out.get("briefs") or {}
    print(f"briefs:\n  {b.get('status', '-'):8} made={b.get('made', 0)} {b.get('reason', '')}")
    return rc


async def _plan(fmt: str, as_of: date | None) -> int:
    import json

    from ode.scoring import plan as planmod

    config = load_config()
    db = await Database.connect(get_secrets().database_url)
    day = as_of or datetime.now(UTC).date()
    try:
        plan = await planmod.build_plan(db, config, day)
        if not plan:
            print("no actions yet — run `ode daily` first", file=sys.stderr)
            return 1
        if fmt == "json":
            print(json.dumps(planmod.plan_as_dicts(plan), ensure_ascii=False, default=str,
                             indent=2))
        elif fmt == "md":
            briefs = await planmod.attach_briefs(db, plan,
                                                 int(config.gemini.get("reuse_days", 14)))
            from ode.backtest import validation_note
            print(planmod.render_plan_markdown(plan, day, briefs, await validation_note(db)),
                  end="")
        else:
            print(planmod.render_plan_table(plan))
        return 0
    finally:
        await db.close()


async def _step(fn_path: str) -> int:
    """Run one optional pipeline step (module:function(db, config, secrets, client))."""
    import importlib

    from ode.pipeline import http_client, run_with_db

    mod, _, name = fn_path.partition(":")
    fn = getattr(importlib.import_module(mod), name)
    config, secrets = load_config(), get_secrets()

    async def run(db: Database) -> dict:
        async with http_client(config) as client:
            return await fn(db, config, secrets, client)
    r = await run_with_db(config, secrets, run)
    print("  " + "  ".join(f"{k}={v}" for k, v in r.items()))
    return 1 if r.get("status") == "error" else 0


async def _competitors(refresh: bool) -> int:
    if refresh:
        from ode import competitors
        from ode.pipeline import http_client

        config, secrets = load_config(), get_secrets()

        async def go(db: Database) -> int:
            async with http_client(config) as client:
                r = await competitors.watch_job(config, secrets, db, client)
            print("  " + "  ".join(f"{k}={v}" for k, v in r.items()))
            return 0
        await _with_db(go)

    async def run(db: Database) -> int:
        rows = await db.pool.fetch(
            "SELECT e.kind, e.market, e.detail, e.seen_at, t.canonical_name FROM "
            "competitor_events e LEFT JOIN topics t ON t.id = e.topic_id "
            "ORDER BY e.seen_at DESC LIMIT 40")
        if not rows:
            print("no competitor events yet")
        for r in rows:
            print(f"  {r['seen_at']:%Y-%m-%d} {r['kind']:13} {r['market']:6} "
                  f"{r['canonical_name']}  {str(r['detail'])[:90]}")
        return 0
    return await _with_db(run)


async def _niches(refresh: bool, limit: int) -> int:
    from ode import clusters

    if refresh:
        rc = await _step("ode.clusters:cluster_job")
        if rc:
            return rc

    async def run(db: Database) -> int:
        print(clusters.render_niches(await clusters.load_niches(db, limit)))
        return 0
    return await _with_db(run)


async def _mine() -> int:
    from ode.pipeline import mine

    out = await mine(load_config(), get_secrets())
    for name, r in out.items():
        print(f"  {r.get('status', '-'):8} {name:12} " + " ".join(
            f"{k}={v}" for k, v in r.items() if k != "status"))
    return 1 if all(r.get("status") == "error" for r in out.values()) else 0


async def _backtest() -> int:
    from ode.backtest import run_backtest

    config = load_config()

    async def run(db: Database) -> int:
        r = await run_backtest(config, db)
        if r["status"] != "ok":
            print(f"  {r['status']}: {r.get('reason', '')}")
            return 0
        for c, x in r["per_cutoff"].items():
            lift = f"{x['lift']:.2f}x" if x["lift"] is not None else "-"
            print(f"  cutoff {c}: top-{x['k']} grew {x['hits']}/{x['k']} = {x['precision']:.0%}"
                  f" · random {x['base_rate']:.0%} · lift {lift} · universe {x['universe']}")
        print(f"\n  mean precision {r['precision']:.0%} vs random {r['base_rate']:.0%}"
              f" · lift {r['lift']}x · weights "
              + ("ADOPTED (better on most cut-offs)" if r["weights_adopted"]
                 else "unchanged (no clear improvement)"))
        return 0
    return await _with_db(run)


async def _export(send: bool) -> int:
    import json

    from ode import export

    config, secrets = load_config(), get_secrets()

    async def run(db: Database) -> int:
        if send:
            from ode.pipeline import http_client
            async with http_client(config) as client:
                r = await export.export_job(config, secrets, db, client)
            print("  " + "  ".join(f"{k}={v}" for k, v in r.items()))
            return 1 if r["status"] == "error" else 0
        pack = await export.make_pack(db, config, datetime.now(UTC).date())
        print(json.dumps(pack, ensure_ascii=False, default=str, indent=2))
        return 0 if pack["items"] else 1
    return await _with_db(run)


async def _briefs() -> int:
    from ode.briefs import run_briefs

    r = await run_briefs(load_config(), get_secrets())
    print(f"  {r['status']:8} briefs made={r.get('made', 0)} errors={r.get('errors', 0)} "
          f"{r.get('reason', '')}")
    return 1 if r["status"] == "error" else 0


async def _realtime(job: str) -> int:
    from ode.pipeline import http_client
    from ode.realtime import runner
    from ode.realtime.social import social_job

    fn = {"hourly": runner.hourly_job, "trends": runner.trends_job,
          "breakout": runner.breakout_job, "plan": runner.plan_message_job,
          "social": social_job}[job]
    config, secrets = load_config(), get_secrets()
    db = await Database.connect(secrets.database_url)
    try:
        await db.init_schema()
        async with http_client(config) as client:
            result = await fn(config, secrets, db, client)
        print("  " + "  ".join(f"{k}={v}" for k, v in result.items()))
        return 1 if result.get("status") == "error" else 0
    finally:
        await db.close()


async def _alerts(limit: int) -> int:
    import json

    db = await Database.connect(get_secrets().database_url)
    try:
        rows = await db.pool.fetch(
            "SELECT kind, key, sent_at, payload FROM alerts_sent ORDER BY sent_at DESC LIMIT $1",
            limit)
        if not rows:
            print("no alerts yet")
        for r in rows:
            p = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"] or "{}")
            first = (p.get("text") or "").splitlines()
            print(f"  {r['sent_at']:%Y-%m-%d %H:%M} {r['kind']:9} {first[0] if first else r['key']}")
        return 0
    finally:
        await db.close()


async def _with_db(fn):
    db = await Database.connect(get_secrets().database_url)
    try:
        await db.init_schema()
        return await fn(db)
    finally:
        await db.close()


async def _done(action: str, status: str, hours: float | None, note: str | None) -> int:
    from ode import worklog

    async def run(db: Database) -> int:
        try:
            r = await worklog.mark(db, action, status, hours=hours, note=note)
        except worklog.UnknownAction as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"  {r['status']:9} {r['action_id']} {r['platform']} {r['market']} {r['topic']}")
        return 0
    return await _with_db(run)


async def _result(action: str, views: int | None, revenue: float | None,
                  hours: float | None) -> int:
    from ode import worklog

    async def run(db: Database) -> int:
        try:
            r = await worklog.record_result(db, action, views=views, revenue=revenue,
                                            hours=hours)
        except worklog.UnknownAction as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"  {r['action_id']} views={r['views']} revenue=${r['revenue_usd'] or 0:,.2f} "
              f"hours={r['hours']}")
        return 0
    return await _with_db(run)


async def _worklog(limit: int) -> int:
    from ode import worklog

    async def run(db: Database) -> int:
        rows = await worklog.entries(db, limit)
        if not rows:
            print("work log empty — after starting a plan item: ode done <id>")
            return 0
        total_rev = sum(r["revenue_usd"] or 0 for r in rows)
        total_h = sum(r["hours"] or 0 for r in rows)
        for r in rows:
            rev = f"${r['revenue_usd']:,.2f}" if r["revenue_usd"] is not None else "-"
            print(f"  {r['action_id']} {r['status']:9} {r['platform']:8} {r['market']:6} "
                  f"views={r['views'] or '-':>8} rev={rev:>9} h={r['hours'] or '-':>4} "
                  f"{r['topic']}")
        print(f"\n  total revenue ${total_rev:,.2f} · {total_h:.0f} hours"
              + (f" · ${total_rev / total_h:,.2f}/hour" if total_h else ""))
        config = load_config()
        values, plat = await worklog.calibration(db, config)
        changed = {m: v for m, v in values.items() if abs(v - config.market_value(m)) > 1e-9}
        if changed:
            print("  learned market values: " + ", ".join(
                f"{m} {config.market_value(m):.2f}->{v:.2f}" for m, v in sorted(changed.items())))
        if plat:
            print("  learned platform weights: " + ", ".join(
                f"{p} x{v:.2f}" for p, v in sorted(plat.items())))
        return 0
    return await _with_db(run)


async def _track(url: str, action: str | None, platform: str | None, keyword: str | None,
                 market: str | None) -> int:
    from ode import content, worklog

    async def run(db: Database) -> int:
        try:
            r = await content.add_content(db, url, action_id=action, platform=platform,
                                          keyword=keyword, market=market)
        except (content.ContentError, worklog.UnknownAction) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"  tracking #{r['id']} {r['platform']} {r['url']}"
              + (f" (keyword: {r['keyword']})" if r["keyword"] else ""))
        return 0
    return await _with_db(run)


async def _content(refresh: bool) -> int:
    import json

    from ode import content

    config, secrets = load_config(), get_secrets()

    async def run(db: Database) -> int:
        if refresh:
            from ode.pipeline import http_client
            async with http_client(config) as client:
                r = await content.content_job(config, secrets, db, client)
            print("  " + "  ".join(f"{k}={v}" for k, v in r.items()))
        rows = await db.pool.fetch(
            "SELECT c.*, (SELECT views FROM content_stats s WHERE s.content_id = c.id "
            "AND views IS NOT NULL ORDER BY date DESC LIMIT 1) AS views "
            "FROM my_content c WHERE active ORDER BY added_at DESC")
        if not rows:
            print("nothing tracked — after publishing: ode track <url> --id <plan id>")
        for r in rows:
            d = r["verdict_detail"] if isinstance(r["verdict_detail"], dict) else json.loads(
                r["verdict_detail"] or "{}")
            icon = content.ICON.get(r["verdict"] or "UNKNOWN", "")
            print(f"  #{r['id']:<4} {icon} {r['verdict'] or '-':10} {r['platform']:8} "
                  f"{(r['title'] or r['url'])[:60]}")
            if d.get("text"):
                print(f"         {d['text']}")
            for s in (d.get("suggestions") or [])[:5]:
                print(f"         → {s}")
        return 0
    return await _with_db(run)


async def _untrack(cid: int) -> int:
    from ode import content

    async def run(db: Database) -> int:
        ok = await content.untrack(db, cid)
        print("  untracked" if ok else "  no such item")
        return 0 if ok else 1
    return await _with_db(run)


async def _week(hours: float | None, save: bool) -> int:
    from ode.scoring import portfolio

    config = load_config()

    async def run(db: Database) -> int:
        if hours is not None and save:
            await portfolio.save_hours(db, hours)
        week = await portfolio.build_week(db, config, datetime.now(UTC).date(), hours)
        if not week.items:
            print("nothing to plan yet — run `ode daily` first", file=sys.stderr)
            return 1
        print(portfolio.render_week(week, int(config.portfolio.get("days", 6))))
        return 0
    return await _with_db(run)


async def _notify_test() -> int:
    from ode.notify import Notifier
    from ode.pipeline import http_client

    config, secrets = load_config(), get_secrets()
    db = await Database.connect(secrets.database_url)
    try:
        await db.init_schema()
        async with http_client(config) as client:
            n = Notifier(config, secrets, client, db)
            if not n.enabled:
                print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set", file=sys.stderr)
                return 1
            await n._send("✅ ODE connected. Alerts will arrive here.")
        print("  sent")
        return 0
    finally:
        await db.close()


async def _notify(text: str) -> int:
    """Plain Telegram message; needs no database (the DB may be what failed)."""
    from ode.notify import Notifier, html_escape
    from ode.pipeline import http_client

    config, secrets = load_config(), get_secrets()
    async with http_client(config) as client:
        n = Notifier(config, secrets, client, None)  # type: ignore[arg-type]
        if not n.enabled:
            print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set", file=sys.stderr)
            return 1
        await n._send(html_escape(text))
    return 0


async def _scheduler() -> int:
    from ode.scheduler import main as sched_main

    return await sched_main(load_config(), get_secrets())


async def _score(as_of: date | None, markets: list[str] | None) -> int:
    from ode.pipeline import score

    return _print_scoring(await score(load_config(), get_secrets(), as_of=as_of, markets=markets))


async def _gems(market: str | None, limit: int, fmt: str, min_conf: float, unknown: bool,
                as_of: date | None) -> int:
    from ode.backtest import validation_note
    from ode.scoring import report

    config = load_config()
    db = await Database.connect(get_secrets().database_url)
    day = as_of or datetime.now(UTC).date()
    try:
        gems = await report.load_gems(db, day, market=market, limit=limit,
                                      min_confidence=min_conf, include_unknown=unknown)
        if not gems:
            print("no scores yet — run `ode daily` (or `ode score`)", file=sys.stderr)
            return 1
        if fmt == "csv":
            print(report.render_csv(gems), end="")
        elif fmt == "md":
            gaps = await report.cross_language_gaps(db, day, [market] if market
                                                    else config.market_codes)
            print(report.render_markdown(gems, gaps, day, await validation_note(db)), end="")
        else:
            print(await validation_note(db))
            print(report.render_table(gems))
        return 0
    finally:
        await db.close()


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
    sub.add_parser("daily", help="discover -> collect -> score -> miners -> briefs (cron)")
    d = sub.add_parser("discover", help="refresh watchlists")
    d.add_argument("--market", action="append")
    c = sub.add_parser("collect", help="run collectors")
    c.add_argument("--source", action="append", help="limit to source(s); repeatable")
    c.add_argument("--market", action="append", help="limit to market(s); repeatable")
    s = sub.add_parser("status", help="recent runs + data summary")
    s.add_argument("--limit", type=int, default=30)
    sub.add_parser("watchlist", help="what is tracked, per market")
    sc = sub.add_parser("score", help="(re)compute scores from stored data")
    sc.add_argument("--as-of", type=date.fromisoformat, help="YYYY-MM-DD (default: today)")
    sc.add_argument("--market", action="append")
    g = sub.add_parser("gems", help="the ranked shortlist")
    g.add_argument("--market")
    g.add_argument("--limit", type=int, default=30)
    g.add_argument("--format", choices=["table", "md", "csv"], default="table")
    g.add_argument("--min-confidence", type=float, default=0.0)
    g.add_argument("--include-unknown", action="store_true",
                   help="also list topics with too little history for a momentum phase")
    g.add_argument("--as-of", type=date.fromisoformat)
    pl = sub.add_parser("plan", help="what to do now: platform + language + why + steps")
    pl.add_argument("--format", choices=["table", "md", "json"], default="table")
    pl.add_argument("--as-of", type=date.fromisoformat)
    sub.add_parser("briefs", help="Gemini briefs for today's top actions (GEMINI_API_KEY)")
    co = sub.add_parser("competitors", help="competitor events on your topics")
    co.add_argument("--refresh", action="store_true", help="check now")
    ni = sub.add_parser("niches", help="related topics grouped into channel / site niches")
    ni.add_argument("--refresh", action="store_true", help="re-embed + re-cluster now")
    ni.add_argument("--limit", type=int, default=20)
    sub.add_parser("langpacks", help="per-language keyword / titles / questions for top items")
    sub.add_parser("seo", help="DataForSEO: Google volume / difficulty / SERP for top items")
    sub.add_parser("mine", help="questions, 1-star reviews, viral formats for today's plan")
    sub.add_parser("backtest", help="precision@k of past rankings vs random; learn weights")
    ex = sub.add_parser("export", help="production pack JSON for n8n / automation")
    ex.add_argument("--send", action="store_true",
                    help="POST to N8N_WEBHOOK_URL (once per day) + write reports/pack-latest.json")
    sub.add_parser("scheduler", help="always-on: run every job on its schedule, forever")
    wb = sub.add_parser("web", help="phone dashboard (needs DASHBOARD_PASSWORD)")
    wb.add_argument("--host", default="127.0.0.1")
    wb.add_argument("--port", type=int, default=8080)
    rt = sub.add_parser("realtime", help="run one realtime job now")
    rt.add_argument("job", choices=["hourly", "trends", "breakout", "plan", "social"])
    dn = sub.add_parser("done", help="mark a plan item as started/published/dropped")
    dn.add_argument("id")
    dn.add_argument("--status", choices=["started", "published", "dropped"], default="started")
    dn.add_argument("--hours", type=float)
    dn.add_argument("--note")
    rs = sub.add_parser("result", help="log views/revenue so far for a plan item")
    rs.add_argument("id")
    rs.add_argument("--views", type=int)
    rs.add_argument("--revenue", type=float, help="USD earned so far")
    rs.add_argument("--hours", type=float, help="hours you spent in total")
    wk = sub.add_parser("week", help="this week's best mix of work for your hours")
    wk.add_argument("--hours", type=float)
    wk.add_argument("--save", action="store_true", help="remember --hours")
    tr = sub.add_parser("track", help="track your published video / page / app")
    tr.add_argument("url")
    tr.add_argument("--id", dest="action", help="the plan item id it came from")
    tr.add_argument("--platform", choices=["YOUTUBE", "SHORTS", "BLOG", "TOOLSITE", "APP"])
    tr.add_argument("--keyword", help="Google keyword the page should rank for")
    tr.add_argument("--market")
    ct = sub.add_parser("content", help="your tracked content with verdicts")
    ct.add_argument("--refresh", action="store_true", help="fetch fresh stats first")
    ut = sub.add_parser("untrack", help="stop tracking a content item")
    ut.add_argument("cid", type=int)
    wl = sub.add_parser("worklog", help="your work, results and what was learned")
    wl.add_argument("--limit", type=int, default=100)
    al = sub.add_parser("alerts", help="recent alerts")
    al.add_argument("--limit", type=int, default=20)
    sub.add_parser("notify-test", help="send a Telegram test message")
    nt = sub.add_parser("notify", help="send a Telegram message")
    nt.add_argument("text")
    args = parser.parse_args(argv)

    commands = {
        "doctor": _doctor,
        "initdb": _initdb,
        "daily": _daily,
        "discover": lambda: _discover(args.market),
        "collect": lambda: _collect(args.source, args.market),
        "status": lambda: _status(args.limit),
        "watchlist": _watchlist,
        "score": lambda: _score(args.as_of, args.market),
        "plan": lambda: _plan(args.format, args.as_of),
        "briefs": _briefs,
        "export": lambda: _export(args.send),
        "backtest": _backtest,
        "mine": _mine,
        "seo": lambda: _step("ode.dataforseo:run_verify"),
        "langpacks": lambda: _step("ode.langpacks:langpacks_job"),
        "niches": lambda: _niches(args.refresh, args.limit),
        "competitors": lambda: _competitors(args.refresh),
        "scheduler": _scheduler,
        "realtime": lambda: _realtime(args.job),
        "alerts": lambda: _alerts(args.limit),
        "done": lambda: _done(args.id, args.status, args.hours, args.note),
        "result": lambda: _result(args.id, args.views, args.revenue, args.hours),
        "worklog": lambda: _worklog(args.limit),
        "week": lambda: _week(args.hours, args.save),
        "track": lambda: _track(args.url, args.action, args.platform, args.keyword, args.market),
        "content": lambda: _content(args.refresh),
        "untrack": lambda: _untrack(args.cid),
        "notify-test": _notify_test,
        "notify": lambda: _notify(args.text),
        "gems": lambda: _gems(args.market, args.limit, args.format, args.min_confidence,
                              args.include_unknown, args.as_of),
    }
    if args.cmd == "web":  # uvicorn runs its own event loop
        from ode.web import serve
        try:
            return serve(load_config(), get_secrets(), args.host, args.port)
        except ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
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
