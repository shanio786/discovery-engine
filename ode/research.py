"""Attach mined research to plan items: long-tail questions (Google
Autocomplete), 1-star complaints about the top Play Store apps, and viral
formats proven by small channels. All read from the DB; filled by the miners."""

from __future__ import annotations

import json
from typing import Any

from ode.db import Database


async def attach_research(db: Database, plan: list[Any], *, questions: int = 12,
                          complaints: int = 5, formats: int = 3) -> None:
    keys = [(a.topic_id, a.market) for a in plan if a.topic_id is not None]
    if not keys:
        return
    tids = list({k[0] for k in keys})
    q_rows = await db.pool.fetch(
        "SELECT topic_id, market, suggestion FROM longtail WHERE topic_id = ANY($1) "
        "ORDER BY fetched_at DESC, length(suggestion)", tids)
    c_rows = await db.pool.fetch(
        "SELECT topic_id, market, content FROM app_complaints WHERE topic_id = ANY($1) "
        "ORDER BY at DESC NULLS LAST", tids)
    f_rows = await db.pool.fetch(
        "SELECT f.topic_id, f.market, f.channel_id, c.title AS channel, f.outlier_count, "
        "f.avg_multiple, f.titles, f.formula FROM formats f "
        "LEFT JOIN channels c ON c.id = f.channel_id WHERE f.topic_id = ANY($1) "
        "ORDER BY f.outlier_count DESC, f.avg_multiple DESC", tids)
    qs: dict[tuple[int, str], list[str]] = {}
    for r in q_rows:
        qs.setdefault((r["topic_id"], r["market"]), []).append(r["suggestion"])
    cs: dict[tuple[int, str], list[str]] = {}
    for r in c_rows:
        cs.setdefault((r["topic_id"], r["market"]), []).append(r["content"])
    fs: dict[int, list[dict[str, Any]]] = {}
    for r in f_rows:
        titles = r["titles"] if isinstance(r["titles"], list) else json.loads(r["titles"])
        fs.setdefault(r["topic_id"], []).append({
            "channel": r["channel"] or r["channel_id"], "market": r["market"],
            "outliers": r["outlier_count"], "avg_multiple": r["avg_multiple"],
            "titles": titles[:5], "formula": r["formula"]})
    for a in plan:
        if a.topic_id is None:
            continue
        key = (a.topic_id, a.market)
        # questions / complaints in the item's own market first, else the source market
        src = (a.topic_id, a.source_market) if a.source_market else key
        a.questions = (qs.get(key) or qs.get(src) or [])[:questions]
        if a.platform in ("APP", "TOOLSITE"):
            a.complaints = (cs.get(key) or cs.get(src) or [])[:complaints]
        if a.platform in ("YOUTUBE", "SHORTS"):
            same = [f for f in fs.get(a.topic_id, []) if f["market"] == a.market]
            a.formats = (same or fs.get(a.topic_id, []))[:formats]
