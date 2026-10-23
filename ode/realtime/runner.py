"""Realtime jobs: hourly Wikipedia spikes, Google Trends, breakout checks,
and the morning plan message. Each job records itself in collector_runs and
never raises (the scheduler must keep running)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ode.collectors.base import make_fetcher
from ode.config import Config, Secrets
from ode.db import Database
from ode.discovery import Discoverer
from ode.logging import get_logger
from ode.notify import Notifier, html_escape
from ode.realtime.hourly import HourlyIngest, Spike
from ode.realtime.trends import DEFAULT_URL, fetch_trends, wiki_match
from ode.scoring.actions import LANGUAGE_NAME, PLATFORM_LABEL

log = get_logger(__name__)


def _wiki_link(project: str, title: str) -> str:
    return f"https://{project}.org/wiki/{title.replace(' ', '_')}"


async def _record(db: Database, job: str, status: str, items: int, detail: dict) -> None:
    run_id = await db.start_run(job, None)
    await db.finish_run(run_id, status=status, items=items, detail=detail)


async def hourly_job(config: Config, secrets: Secrets, db: Database, client) -> dict[str, Any]:
    cfg = config.realtime.get("hourly") or {}
    if not cfg.get("enabled", True):
        return {"status": "skipped", "reason": "realtime.hourly.enabled = false"}
    ingest = HourlyIngest(config, db, client)
    hours = await ingest.catch_up()
    latest = await db.pool.fetchval("SELECT max(hour) FROM hourly_ingested")
    if latest is None:
        await _record(db, "realtime_hourly", "skipped", 0, {"reason": "no dump yet"})
        return {"status": "skipped", "reason": "no hourly dump available yet"}
    spikes = await ingest.spikes(latest)
    by_project = {m.wiki_project: m for m in config.markets()}
    disc = Discoverer(config, client, db)
    notifier = Notifier(config, secrets, client, db)
    cooldown = timedelta(hours=int(config.alerts.get("realtime_cooldown_hours", 24)))
    sent = 0
    admitted: list[dict] = []
    limit = int(cfg.get("max_alerts_per_run", 10))
    # one market (= wiki) at a time so admission batches stay small
    grouped: dict[str, list[Spike]] = {}
    for s in spikes[: limit * 5]:
        grouped.setdefault(s.wiki, []).append(s)
    for project, items in grouped.items():
        market = by_project.get(project)
        if market is None:
            continue
        results = await disc.admit_titles(market, [s.title for s in items], "realtime",
                                          include_people=bool(cfg.get("include_people")))
        ok = {r["title"]: r for r in results if r["active"]}
        for s in items:
            r = ok.get(s.title)
            if not r or sent >= limit:
                continue
            admitted.append({"market": market.code, "title": r["canonical"], "label": s.label})
            lang = LANGUAGE_NAME.get(market.language, market.language)
            text = (f"⚡ <b>Abhi uchal raha hai ({lang}, {market.code})</b>\n"
                    f"<b>{html_escape(r['canonical'])}</b> — {html_escape(s.label)}\n"
                    f"Pehle 48 ghante sab se qeemti: AI short/video abhi banao.\n"
                    f"{_wiki_link(project, r['canonical'])}")
            if await notifier.send_once(f"realtime:{market.code}:{r['canonical']}", "realtime",
                                        text, cooldown=cooldown, payload={"spike": s.label}):
                sent += 1
    out = {"status": "ok", "hours_ingested": len(hours), "spikes": len(spikes),
           "admitted": len(admitted), "alerts": sent}
    await _record(db, "realtime_hourly", "ok", len(admitted), out)
    return out


async def trends_job(config: Config, secrets: Secrets, db: Database, client) -> dict[str, Any]:
    cfg = config.realtime.get("trends") or {}
    if not cfg.get("enabled", True):
        return {"status": "skipped", "reason": "realtime.trends.enabled = false"}
    fetcher = make_fetcher(config, client, "trends")
    wiki = make_fetcher(config, client, "wikipedia")
    disc = Discoverer(config, client, db)
    notifier = Notifier(config, secrets, client, db)
    cooldown = timedelta(hours=int(config.alerts.get("trends_cooldown_hours", 24)))
    min_traffic = int(cfg.get("min_traffic", 2000))
    limit = int(cfg.get("max_alerts_per_run", 5))
    seen_regions: set[str] = set()
    totals = {"items": 0, "matched": 0, "alerts": 0, "errors": 0}
    for market in config.markets():
        if market.region in seen_regions:  # one feed per country
            continue
        seen_regions.add(market.region)
        try:
            items = await fetch_trends(fetcher, market, cfg.get("url", DEFAULT_URL))
        except Exception as exc:  # noqa: BLE001 - one country must not stop the others
            totals["errors"] += 1
            log.warning("trends.fetch_failed", market=market.code, error=str(exc)[:200])
            continue
        totals["items"] += len(items)
        hot = [i for i in items if (i.traffic or 0) >= min_traffic]
        for item in hot:
            await db.pool.execute(
                """
                INSERT INTO trend_items (market, title, traffic, peak_traffic)
                VALUES ($1, $2, $3, $3)
                ON CONFLICT (market, title) DO UPDATE SET last_seen = now(),
                    traffic = EXCLUDED.traffic,
                    peak_traffic = GREATEST(trend_items.peak_traffic, EXCLUDED.traffic)
                """,
                market.code, item.title, item.traffic)
        sent = 0
        for item in sorted(hot, key=lambda i: -(i.traffic or 0)):
            try:
                title = await wiki_match(wiki, market, item.title)
            except Exception:  # noqa: BLE001
                title = None
            active = None
            if title and cfg.get("add_to_watchlist", True):
                res = await disc.admit_titles(market, [title], "trend")
                active = next((r for r in res if r["active"]), None)
                if active:
                    totals["matched"] += 1
                    await db.pool.execute("UPDATE trend_items SET wiki_title = $3 "
                                          "WHERE market = $1 AND title = $2",
                                          market.code, item.title, active["canonical"])
            if sent >= limit or (title and not active):
                continue  # matched an excluded topic (person, film …): don't alert
            news = ("\n" + "\n".join(f"• {html_escape(n)}" for n in item.news)) if item.news else ""
            text = (f"🔥 <b>Trending in {market.region}</b>: <b>{html_escape(item.title)}</b> "
                    f"(~{item.traffic:,}+ searches){news}\n"
                    "Fast AI Shorts/TikTok/Reels ka mauqa." +
                    (f"\n{_wiki_link(market.wiki_project, active['canonical'])}" if active else ""))
            if await notifier.send_once(f"trend:{market.region}:{item.title.casefold()}",
                                        "trend", text, cooldown=cooldown,
                                        payload={"traffic": item.traffic}):
                sent += 1
        totals["alerts"] += sent
    status = "error" if totals["errors"] and not totals["items"] else "ok"
    await _record(db, "realtime_trends", status, totals["items"], totals)
    return {"status": status, **totals}


async def breakout_job(config: Config, secrets: Secrets, db: Database, client) -> dict[str, Any]:
    """Spec alert: BREAKOUT with a 7d-vs-prior-7d spike > 3x and little competition."""
    a = config.alerts.get("breakout") or {}
    rows = await db.pool.fetch(
        """
        WITH latest AS (SELECT market, max(date) AS d FROM scores GROUP BY market)
        SELECT s.market, s.spike_ratio, s.composite, s.details, t.topic_key
        FROM scores s JOIN latest l ON l.market = s.market AND l.d = s.date
        JOIN topics t ON t.id = s.topic_id
        WHERE s.phase = 'BREAKOUT' AND s.spike_ratio >= $1
        ORDER BY s.composite DESC LIMIT 50
        """,
        float(a.get("spike_ratio", 3.0)))
    notifier = Notifier(config, secrets, client, db)
    cooldown = timedelta(days=int(a.get("cooldown_days", 7)))
    max_comp = float(a.get("max_competing_videos", 20))
    sent = 0
    for r in rows:
        d = r["details"] if isinstance(r["details"], dict) else json.loads(r["details"])
        comp = d.get("competing_videos_180d")
        if comp is not None and comp >= max_comp:
            continue
        lang = LANGUAGE_NAME.get(config.market(r["market"]).language, r["market"])
        comp_txt = "competition abhi naapa nahi" if comp is None else f"sirf {int(comp)} videos"
        text = (f"🚀 <b>BREAKOUT ({lang}, {r['market']})</b>: <b>{html_escape(d.get('name', ''))}</b>\n"
                f"pichhle 7 din {r['spike_ratio']:.1f}x · {d.get('mean30') or 0:,.0f} views/day · "
                f"{comp_txt}\nAI video + blog abhi shuru karo.")
        if await notifier.send_once(f"breakout:{r['market']}:{r['topic_key']}", "breakout", text,
                                    cooldown=cooldown):
            sent += 1
    out = {"status": "ok", "candidates": len(rows), "alerts": sent}
    await _record(db, "alerts_breakout", "ok", sent, out)
    return out


async def plan_message_job(config: Config, secrets: Secrets, db: Database, client
                           ) -> dict[str, Any]:
    """Morning Telegram: today's top actions, one line each + where else it's open."""
    from ode.scoring.plan import build_plan

    today = datetime.now(UTC).date()
    plan = await build_plan(db, config, today)
    if not plan:
        return {"status": "skipped", "reason": "no plan yet"}
    n = int(config.alerts.get("plan_top_n", 5))
    lines = [f"☀️ <b>Aaj kya karna hai — {today}</b>"]
    for i, a in enumerate(plan[:n], 1):
        lang = LANGUAGE_NAME.get(a.language, a.language)
        also = (" · also: " + ", ".join(LANGUAGE_NAME.get(lg, lg) for lg, _ in a.also_in[:4])
                if a.also_in else "")
        lines.append(f"\n<b>{i}. {html_escape(PLATFORM_LABEL[a.platform])} · {lang}</b>\n"
                     f"{html_escape(a.topic)} — "
                     + (f"💵 {a.dollars.label()}, " if a.dollars and a.dollars.high else "")
                     + f"ease {a.ease:.0f}{also}\n"
                     f"↳ {html_escape(a.why[0] if a.why else '')}")
    lines.append("\nPoori list + steps: reports/plan-latest.md")
    notifier = Notifier(config, secrets, client, db)
    ok = await notifier.send_once(f"plan:{today}", "plan", "\n".join(lines),
                                  cooldown=timedelta(hours=20))
    return {"status": "ok" if ok else "skipped", "items": min(n, len(plan))}


async def reports_job(config: Config, secrets: Secrets, db: Database, client) -> dict[str, Any]:
    """Write today's plan + gems as Markdown/CSV into ODE_REPORTS_DIR (mounted
    from the host as ./reports), with *-latest copies for easy reading."""
    import os
    from pathlib import Path

    from ode.scoring import plan as planmod
    from ode.scoring import report

    out_dir = Path(os.environ.get("ODE_REPORTS_DIR", "reports"))
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(UTC).date()
    plan = await planmod.build_plan(db, config, today)
    briefs = await planmod.attach_briefs(db, plan, int(config.gemini.get("reuse_days", 14)))
    from ode.backtest import validation_note

    note = await validation_note(db)
    files = {f"plan-{today}.md": planmod.render_plan_markdown(plan, today, briefs, note)}
    gems = await report.load_gems(db, today, limit=40)
    if gems:
        gaps = await report.cross_language_gaps(db, today, config.market_codes)
        files[f"gems-{today}.md"] = report.render_markdown(gems, gaps, today, note)
        files[f"gems-{today}.csv"] = report.render_csv(
            await report.load_gems(db, today, limit=200))
    for name, text in files.items():
        (out_dir / name).write_text(text, encoding="utf-8")
        latest = name.replace(str(today), "latest")
        (out_dir / latest).write_text(text, encoding="utf-8")
    cutoff = today - timedelta(days=60)
    for f in out_dir.glob("*-20*"):
        try:
            if date.fromisoformat(f.stem.split("-", 1)[1][:10]) < cutoff:
                f.unlink()
        except ValueError:
            continue
    return {"status": "ok", "files": sorted(files), "dir": str(out_dir)}
