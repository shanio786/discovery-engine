"""`ode plan`: today's action list across every market and platform."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from ode.config import Config
from ode.db import Database
from ode.scoring.actions import (
    LANGUAGE_NAME,
    PLATFORM_LABEL,
    Action,
    TopicView,
    attach_also_in,
    plan_topic,
    select_plan,
)
from ode.scoring.report import UNVALIDATED


async def load_views(db: Database, config: Config, as_of: date) -> list[TopicView]:
    """Latest scores per market (<= as_of) joined with fresh Play Store metrics."""
    rows = await db.pool.fetch(
        """
        WITH latest AS (SELECT market, max(date) AS d FROM scores WHERE date <= $1 GROUP BY market)
        SELECT s.* FROM scores s JOIN latest l ON l.market = s.market AND l.d = s.date
        WHERE s.market = ANY($2)
        """,
        as_of, config.market_codes,
    )
    ps_rows = await db.pool.fetch(
        """
        SELECT DISTINCT ON (topic_id, market, metric_name) topic_id, market, metric_name, value
        FROM topic_metrics WHERE source = 'playstore' AND date <= $1 AND date > $2
        ORDER BY topic_id, market, metric_name, date DESC
        """,
        as_of, as_of - timedelta(days=30),
    )
    ps: dict[tuple[int, str], dict[str, float]] = defaultdict(dict)
    for r in ps_rows:
        ps[(r["topic_id"], r["market"])][r["metric_name"]] = r["value"]
    views = []
    for r in rows:
        d = r["details"] if isinstance(r["details"], dict) else json.loads(r["details"] or "{}")
        gp = d.get("gap_parts") or {}
        views.append(TopicView(
            market=r["market"], language=config.market(r["market"]).language,
            name=d.get("name") or str(r["topic_id"]), phase=r["phase"], momentum=r["momentum"],
            demand=r["demand"], money=r["money"], money_path=r["money_path"],
            money_paths=d.get("money_paths") or {}, gap=r["gap"], competition=r["competition"],
            confidence=r["confidence"], saturation=r["saturation_risk"],
            mean30=d.get("mean30"), change30=d.get("change30"),
            competing_videos=d.get("competing_videos_180d"),
            competing_shorts=d.get("competing_shorts_180d"), google=d.get("google") or {},
            outlier=gp.get("outlier"),
            unanswered=gp.get("unanswered"), missing_in=d.get("missing_in") or [],
            wiki_langs=d.get("wiki_langs"), market_value=config.market_value(r["market"]),
            playstore=ps.get((r["topic_id"], r["market"]), {}), topic_id=r["topic_id"],
            tutorial=bool(d.get("tutorial")), tool_keyword=d.get("tool_keyword"),
            seasonal=d.get("seasonal"),
        ))
    return views


async def build_plan(db: Database, config: Config, as_of: date, *,
                     save: bool = True) -> list[Action]:
    """Today's plan. Items you already started / published / dropped are left
    out; market values and platform weights are calibrated from your results;
    shown items are saved so their ids stay resolvable."""
    from ode import worklog
    from ode.research import attach_research

    cfg = config.actions
    views = await load_views(db, config, as_of)
    values, platform_mult = await worklog.calibration(db, config)
    lang_market: dict[str, str] = {}
    for m in config.markets():  # first configured market per language
        lang_market.setdefault(m.language, m.code)
    min_conf = float(cfg.get("min_confidence", 0.0))
    done = await worklog.logged_ids(db)
    actions: list[Action] = []
    for v in views:
        if v.confidence < min_conf:
            continue
        v.market_value = values.get(v.market, v.market_value)
        actions += plan_topic(v, config=cfg, market_values=values, language_markets=lang_market)
    # a "be first in language X" guess is moot where X already has its own measured score
    native = {(v.topic_id, v.market) for v in views}
    actions = [a for a in actions
               if not (a.source_market and (a.topic_id, a.market) in native)]
    for a in actions:
        if a.platform in platform_mult:
            a.score = round(a.score * platform_mult[a.platform], 2)
    open_actions = [a for a in actions if a.action_id not in done]
    plan = select_plan(open_actions, top_n=int(cfg.get("top_n", 25)),
                       per_market_max=int(cfg.get("per_market_max", 4)),
                       per_topic_max=int(cfg.get("per_topic_max", 3)))
    attach_also_in(plan, open_actions, int(cfg.get("also_in_max", 6)))
    await attach_research(db, plan)
    if save:
        await worklog.save_plan_items(db, as_of, plan)
    return plan


async def attach_briefs(db: Database, plan: list[Action], days: int = 14) -> dict[int, dict]:
    """Recent Gemini briefs for plan items, keyed by plan index."""
    out: dict[int, dict] = {}
    for i, a in enumerate(plan):
        if a.topic_id is None:
            continue
        row = await db.pool.fetchrow(
            "SELECT titles, outline, format FROM briefs WHERE topic_id = $1 AND platform = $2 "
            "AND generated_at > now() - make_interval(days => $3) "
            "ORDER BY generated_at DESC LIMIT 1",
            a.topic_id, f"{a.platform}:{a.language}", days,
        )
        if row:
            titles = row["titles"] if isinstance(row["titles"], list) else json.loads(row["titles"])
            out[i] = {"titles": titles, "outline": row["outline"], "format": row["format"]}
    return out


def render_plan_markdown(plan: list[Action], as_of: date,
                         briefs: dict[int, dict] | None = None,
                         note: str = UNVALIDATED) -> str:
    briefs = briefs or {}
    out = [f"# Aaj kya karna hai — {as_of}", "",
           ("Har item: **kis platform par, kis language mein, kis topic par**, kyun, aur pehle "
           "steps. Sab se upar wala sab se behtar."), "", f"> {note}", ""]
    if not plan:
        out.append("_Abhi koi action nahi — pehle `ode daily` chalao (60 din ki history chahiye)._")
    for i, a in enumerate(plan, 1):
        out += [f"## {i}. {a.headline}", "",
                f"**Score {a.score:.0f}** · aasani (ease) {a.ease:.0f}/100 · "
                f"value {a.value:.0f}/100 · market `{a.market}` · id `{a.action_id}`"
                + (f" · demand `{a.source_market}` se saabit" if a.source_market else ""), "",
                "**Kyun:**"]
        out += [f"- {w}" for w in a.why]
        if a.also_in:
            langs = ", ".join(f"{LANGUAGE_NAME.get(lg, lg)} (ease {e:.0f})" for lg, e in a.also_in)
            out += ["", f"**AI se in languages mein bhi copy karo (yahan bhi khula hai):** {langs}"]
        out += ["", "**Pehle steps:**"]
        out += [f"{n}. {s}" for n, s in enumerate(a.steps, 1)]
        out += _research_md(a)
        b = briefs.get(i - 1)
        if b:
            out += ["", "**Title ideas (Gemini):**"] + [f"- {t}" for t in b["titles"][:10]]
            if b.get("outline"):
                out += ["", "**Outline:**", "", b["outline"]]
        if a.unverified:
            out += ["", "_Abhi verify nahi hua: " + "; ".join(a.unverified) + "_"]
        out.append("")
    out += ["---", ("_Kaam shuru kiya? `ode done <id>` · kamai aayi? "
            "`ode result <id> --views N --revenue USD` — engine tumhare results se seekhta hai "
            "aur woh item dobara nahi dikhata._"), "", "_Platforms: " + " · ".join(PLATFORM_LABEL.values()) + ". Sab kuch AI aur "
            "automation se banta hai. Affiliate, SaaS aur product selling jaan boojh kar shamil "
            "nahi._"]
    return "\n".join(out) + "\n"


def _research_md(a: Action) -> list[str]:
    out: list[str] = []
    if a.questions:
        out += ["", "**Log yeh sawal poochh rahe hain (har sawal = ek article / video):**"]
        out += [f"- {q}" for q in a.questions]
    if a.complaints:
        out += ["", "**Top apps ki 1-star shikayatein (yehi tumhari feature list hai):**"]
        out += [f"- “{c[:220]}”" for c in a.complaints]
    if a.formats:
        out += ["", "**Saabit viral format (chhote channels jo baar baar chalte hain):**"]
        for f in a.formats:
            mult = f" · avg {f['avg_multiple']:.0f}x subs" if f.get("avg_multiple") else ""
            out.append(f"- {f['channel']}: {f['outliers']} hit videos{mult}")
            if f.get("formula"):
                out.append(f"  - formula: {f['formula']}")
            out += [f"  - “{t}”" for t in f["titles"][:3]]
    return out


def render_plan_table(plan: list[Action]) -> str:
    lines = [(f"{'#':>3} {'id':8} {'score':>6} {'ease':>4} {'value':>5}  {'platform':8} "
              f"{'mkt':6} topic")]
    for i, a in enumerate(plan, 1):
        src = f"  (demand from {a.source_market})" if a.source_market else ""
        lines.append(f"{i:>3} {a.action_id:8} {a.score:>6.0f} {a.ease:>4.0f} {a.value:>5.0f}  "
                     f"{a.platform:8} {a.market:6} {a.topic}{src}")
        lines.append(f"      ↳ {a.why[0] if a.why else ''}")
        if a.also_in:
            lines.append("      ↳ also open in: " + ", ".join(lg for lg, _ in a.also_in))
    return "\n".join(lines)


def plan_as_dicts(plan: list[Action]) -> list[dict[str, Any]]:
    return [a.__dict__ | {"action_id": a.action_id, "headline": a.headline} for a in plan]
