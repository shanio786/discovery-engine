"""Weekly plan: your hours -> the mix of work that should earn the most.

    1. double-down "make 5 more" of your winners, always first
    2. upkeep      things you already run (a channel needs new videos, a site
                   new posts) get their weekly hours (capped so new work
                   always has room); items judged DROP get none
    3. new work    greedy by expected $ per hour (scoring.dollars), with a
                   spread: at most N items per platform and per market, and a
                   bonus for a kind of work not yet in the week (fast trend
                   work + evergreen work)
    4. schedule    hours spread over your working days, big items first;
                   an item may span days

Pure functions + `build_week()` which loads everything from the DB.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ode.scoring.actions import LANGUAGE_NAME, PLATFORM_LABEL, Action

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DEFAULTS: dict[str, Any] = {
    "hours_per_week": 20,
    "days": 6,
    "candidates": 60,
    "max_per_platform": 3,
    "max_per_market": 3,
    "upkeep_hours_per_week": {"YOUTUBE": 3, "SHORTS": 2, "BLOG": 2, "TOOLSITE": 1, "APP": 1},
    "max_upkeep_share": 0.6,
    "mix_bonus": 1.25,
}


@dataclass
class WeekItem:
    kind: str                      # upkeep | more | new
    title: str
    platform: str
    market: str
    hours: float
    action: Action | None = None
    dollars: tuple[float, float] | None = None
    reason: str = ""
    days: list[str] = field(default_factory=list)

    @property
    def mid(self) -> float:
        if not self.dollars:
            return 0.0
        lo, hi = self.dollars
        return (max(lo, 0.01) * max(hi, 0.01)) ** 0.5


@dataclass
class Week:
    budget: float
    items: list[WeekItem]

    @property
    def hours(self) -> float:
        return round(sum(i.hours for i in self.items), 1)

    @property
    def dollars(self) -> tuple[float, float]:
        new = [i for i in self.items if i.dollars and i.kind != "upkeep"]  # NEW income only
        return (round(sum(i.dollars[0] for i in new), 2), round(sum(i.dollars[1] for i in new), 2))


def _cfg(cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for k, v in (cfg or {}).items():
        out[k] = {**out.get(k, {}), **v} if isinstance(v, dict) else v
    return out


def _category(a: Action) -> str:
    fast = a.platform == "SHORTS" or any("BREAKOUT" in w for w in a.why[:2])
    return "fast" if fast else "evergreen"


def choose(candidates: Sequence[Action], budget: float, *, upkeep: Sequence[WeekItem] = (),
           cfg: Mapping[str, Any] | None = None) -> Week:
    """Pure: pick the week's work."""
    c = _cfg(cfg)
    items: list[WeekItem] = []
    left = budget
    per_platform: dict[str, int] = {}
    per_market: dict[str, int] = {}
    cats: set[str] = set()
    pool = [a for a in candidates if a.dollars is not None and a.dollars.hours > 0]
    more = [a for a in pool if a.variant.startswith("more:")]
    new = [a for a in pool if not a.variant.startswith("more:")]

    def take(a: Action, kind: str, why: str) -> None:
        nonlocal left
        items.append(WeekItem(kind, a.topic, a.platform, a.market, a.dollars.hours, a,
                              (a.dollars.low, a.dollars.high), why))
        left -= a.dollars.hours
        per_platform[a.platform] = per_platform.get(a.platform, 0) + 1
        per_market[a.market] = per_market.get(a.market, 0) + 1
        cats.add(_category(a))

    for a in more:  # your winners first: the surest money
        if a.dollars.hours <= left + 1e-9:
            take(a, "more", "tumhara winner: isi par aur")
    cap, used = budget * float(c["max_upkeep_share"]), 0.0
    for u in sorted(upkeep, key=lambda u: -u.mid):
        if used + u.hours <= min(cap, left) + 1e-9:
            items.append(u)
            used += u.hours
    left -= used
    while True:
        best, best_val = None, 0.0
        for a in new:
            if (a.dollars.hours > left + 1e-9
                    or per_platform.get(a.platform, 0) >= int(c["max_per_platform"])
                    or per_market.get(a.market, 0) >= int(c["max_per_market"])):
                continue
            val = a.dollars.per_hour * (float(c["mix_bonus"]) if _category(a) not in cats else 1)
            if val > best_val:
                best, best_val = a, val
        if best is None:
            break
        new.remove(best)
        take(best, "new", f"~${best.dollars.per_hour:,.1f}/ghanta andaza")
    return Week(budget, items)


def schedule(week: Week, days: int = 6) -> dict[str, list[tuple[WeekItem, float]]]:
    """Spread hours over working days (big items first; items may span days)."""
    names = DAYS[: max(1, min(days, 7))]
    per_day = week.budget / len(names)
    out: dict[str, list[tuple[WeekItem, float]]] = {d: [] for d in names}
    free = {d: per_day for d in names}
    order = sorted(week.items, key=lambda i: (i.kind != "more", i.kind == "upkeep", -i.hours))
    di = 0
    for it in order:
        need = it.hours
        it.days = []
        while need > 1e-9 and di < len(names):
            d = names[di]
            chunk = min(need, free[d])
            if chunk > 1e-9:
                out[d].append((it, round(chunk, 1)))
                it.days.append(d)
                free[d] -= chunk
                need -= chunk
            if free[d] <= 1e-9:
                di += 1
        if need > 1e-9:  # rounding leftovers go to the last day
            out[names[-1]].append((it, round(need, 1)))
    return out


def render_week(week: Week, days: int = 6) -> str:
    lo, hi = week.dollars
    sched = schedule(week, days)
    lines = [(f"Is hafte ka plan — {week.hours:g} / {week.budget:g} ghante · "
              f"nayi kamai ka andaza ${lo:,.0f}–${hi:,.0f}/mahina (3–6 mahine baad)"), ""]
    icon = {"more": "🔥", "new": "🆕", "upkeep": "🔁"}
    for i, it in enumerate(week.items, 1):
        lang = LANGUAGE_NAME.get(it.market.split("-")[0], it.market)
        money = f" · ${it.dollars[0]:,.0f}–${it.dollars[1]:,.0f}/mo" if it.dollars else ""
        aid = f" · id {it.action.action_id}" if it.action else ""
        lines.append(f"{i}. {icon[it.kind]} {PLATFORM_LABEL.get(it.platform, it.platform)} · "
                     f"{lang} — {it.title} ({it.hours:g} h{money}{aid})")
        if it.reason:
            lines.append(f"   ↳ {it.reason}")
    lines += ["", "Roz ka schedule:"]
    for d, parts in sched.items():
        if parts:
            lines.append(f"  {d}: " + " · ".join(f"{p.title[:28]} {h:g}h" for p, h in parts))
    return "\n".join(lines)


async def hours_setting(db: Any, config: Any) -> float:
    raw = await db.pool.fetchval("SELECT value FROM settings WHERE key = 'hours_per_week'")
    if raw is not None:
        v = raw if isinstance(raw, int | float) else json.loads(raw)
        return float(v)
    return float(_cfg(config.portfolio)["hours_per_week"])


async def save_hours(db: Any, hours: float) -> None:
    await db.pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('hours_per_week', $1::jsonb, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        json.dumps(hours))


async def upkeep_items(db: Any, config: Any) -> list[WeekItem]:
    """Weekly hours for work you already run (not dropped, not judged DROP)."""
    c = _cfg(config.portfolio)
    rows = await db.pool.fetch(
        """
        SELECT w.*, (SELECT verdict FROM my_content m WHERE m.action_id = w.action_id
                     ORDER BY m.added_at DESC LIMIT 1) AS verdict
        FROM work_log w WHERE w.status IN ('started', 'published')
        ORDER BY w.revenue_usd DESC NULLS LAST, w.updated_at DESC
        """)
    out = []
    for r in rows:
        if r["verdict"] == "DROP":
            continue
        h = float(c["upkeep_hours_per_week"].get(r["platform"], 1))
        rev = r["revenue_usd"]
        months = max(1.0, (r["updated_at"] - r["started_at"]).days / 30.0)
        dollars = (rev / months * 0.8, rev / months * 1.2) if rev else None
        out.append(WeekItem("upkeep", r["topic"], r["platform"], r["market"], h, None, dollars,
                            "jo chal raha hai usko zinda rakho" + (
                                f" ({r['verdict']})" if r["verdict"] else "")))
    return out


async def build_week(db: Any, config: Any, today: date, hours: float | None = None) -> Week:
    from ode.scoring.plan import build_plan

    c = _cfg(config.portfolio)
    budget = hours if hours is not None else await hours_setting(db, config)
    cands = await build_plan(db, config, today, top_n=int(c["candidates"]))
    return choose(cands, budget, upkeep=await upkeep_items(db, config), cfg=c)
