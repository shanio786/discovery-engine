"""`ode gems`: the shortlist a human acts on.

Reads the latest persisted scores (<= as_of) and renders them as a terminal
table, Markdown or CSV. Every row says WHY it scored, and the header states
plainly that scores are unvalidated until the Phase-7 backtest.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from ode.db import Database

PHASE_ICON = {"BREAKOUT": "🚀", "GROWING": "📈", "PEAKING": "⛰", "STABLE": "➖",
              "DECLINING": "📉", "UNKNOWN": "❔"}

UNVALIDATED = ("Scores are UNVALIDATED: the backtest (`ode backtest`, weekly in the "
               "scheduler) has not measured precision@20 yet. Treat this as a ranked shortlist to verify, not a forecast.")


@dataclass
class Gem:
    rank: int
    market: str
    name: str
    phase: str
    composite: float
    demand: float
    momentum: float
    money: float
    money_path: str
    gap: float | None
    competition: float | None
    confidence: float
    saturation: str
    details: dict[str, Any]

    def why(self) -> str:
        d = self.details
        bits = [f"{self.phase.lower()} ({d.get('momentum', '')})"]
        if d.get("mean30") is not None:
            ch = d.get("change30")
            trend = f", {ch:+.0%} vs prior 30d" if ch is not None else ""
            bits.append(f"{d['mean30']:.0f} wiki views/day{trend}")
        paths = d.get("money_paths") or {}
        bits.append(f"{self.money_path}: {paths.get(self.money_path, '')}")
        if d.get("competing_videos_180d") is not None:
            bits.append(f"{int(d['competing_videos_180d'])} YouTube videos in 180d")
        gp = d.get("gap_parts") or {}
        if "outlier" in gp:
            bits.append(f"outlier strength {gp['outlier']:.0f}/100")
        if "unanswered" in gp:
            bits.append(f"{gp['unanswered']:.0f}% Reddit posts unanswered")
        if d.get("missing_in"):
            bits.append(f"no Wikipedia article in: {', '.join(d['missing_in'])}")
        if self.gap is None and self.competition is None:
            bits.append("no YouTube data yet (queued for a probe)")
        return "; ".join(b for b in bits if b)


async def load_gems(db: Database, as_of: date, *, market: str | None = None, limit: int = 30,
                    min_confidence: float = 0.0, include_unknown: bool = False) -> list[Gem]:
    rows = await db.pool.fetch(
        """
        WITH latest AS (
            SELECT market, max(date) AS d FROM scores WHERE date <= $1 GROUP BY market)
        SELECT s.*, row_number() OVER (PARTITION BY s.market ORDER BY s.composite DESC) AS rk
        FROM scores s JOIN latest l ON l.market = s.market AND l.d = s.date
        WHERE ($2::text IS NULL OR s.market = $2)
          AND s.confidence >= $3
          AND ($4 OR s.phase <> 'UNKNOWN')
        ORDER BY s.market, s.composite DESC
        """,
        as_of, market, min_confidence, include_unknown,
    )
    gems: list[Gem] = []
    per_market: dict[str, int] = {}
    for r in rows:
        n = per_market.get(r["market"], 0)
        if n >= limit:
            continue
        per_market[r["market"]] = n + 1
        details = r["details"] if isinstance(r["details"], dict) else json.loads(r["details"] or "{}")
        gems.append(Gem(n + 1, r["market"], details.get("name") or str(r["topic_id"]), r["phase"],
                        r["composite"], r["demand"], r["momentum"], r["money"], r["money_path"],
                        r["gap"], r["competition"], r["confidence"], r["saturation_risk"],
                        details))
    return gems


async def cross_language_gaps(db: Database, as_of: date, markets: list[str],
                              limit: int = 15) -> list[dict[str, Any]]:
    """Strong topics in one market whose Wikipedia has no article in another
    configured market's language: demand proven elsewhere, supply missing here."""
    rows = await db.pool.fetch(
        """
        WITH latest AS (SELECT market, max(date) AS d FROM scores WHERE date <= $1 GROUP BY market)
        SELECT s.market, s.composite, s.phase, s.details
        FROM scores s JOIN latest l ON l.market = s.market AND l.d = s.date
        WHERE s.market = ANY($2) AND s.phase <> 'UNKNOWN'
          AND jsonb_array_length(COALESCE(s.details->'missing_in', '[]'::jsonb)) > 0
        ORDER BY s.composite DESC LIMIT $3
        """,
        as_of, markets, limit,
    )
    out = []
    for r in rows:
        d = r["details"] if isinstance(r["details"], dict) else json.loads(r["details"])
        out.append({"name": d.get("name"), "strong_in": r["market"], "missing_in": d["missing_in"],
                    "phase": r["phase"], "composite": r["composite"],
                    "views": d.get("mean30")})
    return out


def _fmt(v: float | None, spec: str = ".0f") -> str:
    return "–" if v is None else format(v, spec)


def render_table(gems: list[Gem]) -> str:
    lines = []
    market = None
    for g in gems:
        if g.market != market:
            market = g.market
            lines.append(f"\n== {market} ==")
            lines.append(f"{'#':>3} {'topic':34} {'phase':9} {'score':>6} {'D':>4} {'M':>4} "
                         f"{'$':>4} {'path':8} {'gap':>4} {'comp':>4} {'conf':>5} {'saturation':12}")
        lines.append(
            f"{g.rank:>3} {g.name[:34]:34} {g.phase:9} {g.composite:>6.1f} {g.demand:>4.0f} "
            f"{g.momentum:>4.1f} {g.money:>4.0f} {g.money_path:8} {_fmt(g.gap):>4} "
            f"{_fmt(g.competition):>4} {g.confidence:>5.2f} {g.saturation:12}")
        lines.append(f"      ↳ {g.why()}")
    return "\n".join(lines)


def render_markdown(gems: list[Gem], gaps: list[dict[str, Any]], as_of: date,
                    note: str = UNVALIDATED) -> str:
    out = [f"# Opportunity gems — {as_of}", "", f"> {note}", ""]
    market = None
    for g in gems:
        if g.market != market:
            market = g.market
            out += ["", f"## {market}", "",
                    ("| # | Topic | Phase | Score | Demand | Money | Gap | Competition | "
                    "Confidence | Saturation | Why |"),
                    "|---|---|---|---|---|---|---|---|---|---|---|"]
        out.append(
            f"| {g.rank} | **{g.name}** | {PHASE_ICON.get(g.phase, '')} {g.phase} | "
            f"{g.composite:.1f} | {g.demand:.0f} | {g.money:.0f} {g.money_path} | {_fmt(g.gap)} | "
            f"{_fmt(g.competition)} | {g.confidence:.2f} | {g.saturation} | "
            f"{g.why().replace('|', '/')} |")
    if gaps:
        out += ["", "## Cross-language gaps", "",
                "Strong where they are, but the target language has no Wikipedia article yet.", "",
                "| Topic | Strong in | Missing in | Phase | Score | Views/day |",
                "|---|---|---|---|---|---|"]
        for x in gaps:
            out.append(f"| {x['name']} | {x['strong_in']} | {', '.join(x['missing_in'])} | "
                       f"{x['phase']} | {x['composite']:.1f} | {_fmt(x['views'])} |")
    out += ["", ("_Legend: Score = momentum × geometric mean(demand, money, gap, 101 − "
            "competition). Confidence 0–1 = history length + how many parts rest on real data._")]
    return "\n".join(out) + "\n"


def render_csv(gems: list[Gem]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["market", "rank", "topic", "phase", "composite", "demand", "momentum", "money",
                "money_path", "gap", "competition", "confidence", "saturation", "why"])
    for g in gems:
        w.writerow([g.market, g.rank, g.name, g.phase, g.composite, g.demand, g.momentum,
                    g.money, g.money_path, g.gap, g.competition, g.confidence, g.saturation,
                    g.why()])
    return buf.getvalue()
