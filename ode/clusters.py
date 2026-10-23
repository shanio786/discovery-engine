"""Niches: related topics grouped into one channel / site idea.

Every recently scored topic is embedded (multilingual sentence embeddings)
through any OpenAI-compatible /embeddings endpoint:

    EMBEDDINGS_URL + EMBEDDINGS_API_KEY   e.g. your RunPod serverless
        endpoint (worker-infinity-embedding):
        https://api.runpod.ai/v2/<endpoint id>/openai/v1/embeddings
    else GEMINI_API_KEY                   Gemini's OpenAI-compatible endpoint

Vectors are cached per topic (re-embedded only when its text changes), then
grouped with leader clustering (strongest topics first; join the nearest
niche when cosine >= threshold, then one re-assignment pass) — no extra
libraries, fine for thousands of topics on a small VPS.

Each niche of >= `min_size` topics is ranked by total demand, share of
growing topics, openness and money, and gets a name (its strongest topic,
or a short Gemini name). `ode niches`, the dashboard, and plan items show
"this topic belongs to niche X with N related topics": one channel or site
with material for many videos / articles.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import numpy as np

from ode.collectors.base import make_fetcher
from ode.config import Config, Secrets
from ode.db import Database
from ode.logging import get_logger

log = get_logger(__name__)

GEMINI_OPENAI = "https://generativelanguage.googleapis.com/v1beta/openai/embeddings"


# ---------------------------------------------------------------- pure
def normalize(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.where(norms == 0, 1.0, norms)


def leader_cluster(vectors: np.ndarray, threshold: float) -> list[int]:
    """Pure: rows must be ordered strongest-first. Returns a label per row.

    Pass 1: each row joins the most similar centroid if cosine >= threshold,
    else starts a new cluster. Pass 2: every row is re-assigned to its nearest
    final centroid (still subject to the threshold), which removes most of
    the order dependence of a single pass.
    """
    if len(vectors) == 0:
        return []
    v = normalize(np.asarray(vectors, dtype=np.float32))
    centroids: list[np.ndarray] = []
    sums: list[np.ndarray] = []
    labels: list[int] = []
    for row in v:
        if centroids:
            sims = np.stack(centroids) @ row
            j = int(np.argmax(sims))
            if sims[j] >= threshold:
                sums[j] = sums[j] + row
                centroids[j] = sums[j] / np.linalg.norm(sums[j])
                labels.append(j)
                continue
        sums.append(row.copy())
        centroids.append(row.copy())
        labels.append(len(centroids) - 1)
    c = np.stack(centroids)
    sims = v @ c.T
    best = sims.argmax(axis=1)
    out = [int(b) if sims[i, b] >= threshold else labels[i] for i, b in enumerate(best)]
    # renumber densely in first-seen order
    remap: dict[int, int] = {}
    return [remap.setdefault(x, len(remap)) for x in out]


@dataclass
class Member:
    topic_id: int
    name: str
    views: float               # summed Wikipedia views/day over markets
    growing: bool
    openness: float | None     # 101 - competition (best market)
    money: float
    markets: dict[str, float] = field(default_factory=dict)   # market -> views/day


def niche_score(members: Sequence[Member], market_values: dict[str, float]) -> dict[str, Any]:
    """Pure: summary + rank score of one niche."""
    total = sum(m.views for m in members)
    growing = sum(m.growing for m in members) / len(members)
    opens = [m.openness for m in members if m.openness is not None]
    openness = sum(opens) / len(opens) if opens else 50.0
    money = sum(m.money for m in members) / len(members)
    by_market: dict[str, float] = {}
    for m in members:
        for code, v in m.markets.items():
            by_market[code] = by_market.get(code, 0.0) + v * market_values.get(code, 0.5)
    score = (math.log10(1 + total) * (0.5 + growing) * (0.5 + openness / 100)
             * (0.5 + money / 100))
    return {"size": len(members), "views_per_day": round(total), "growing_share":
            round(growing, 2), "openness": round(openness, 1), "money": round(money, 1),
            "best_markets": [c for c, _ in sorted(by_market.items(), key=lambda x: -x[1])[:4]],
            "score": round(score, 3)}


def signature(ids: Sequence[int]) -> str:
    return hashlib.sha1(",".join(map(str, sorted(ids))).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- embeddings
def _endpoint(config: Config, secrets: Secrets) -> tuple[str, str, str] | None:
    """(url, api key, model) or None when no embedding provider is configured."""
    c = config.clusters
    if secrets.embeddings_url and secrets.embeddings_api_key:
        return (secrets.embeddings_url, secrets.embeddings_api_key,
                str(c.get("model", "BAAI/bge-m3")))
    if secrets.gemini_api_key and c.get("gemini_fallback", True):
        return (GEMINI_OPENAI, secrets.gemini_api_key,
                str(c.get("gemini_model", "gemini-embedding-001")))
    return None


async def embed(client: httpx.AsyncClient, config: Config, url: str, key: str, model: str,
                texts: list[str]) -> list[list[float]]:
    """OpenAI-compatible /embeddings, batched; order preserved."""
    fetcher = make_fetcher(config, client, "embeddings")
    out: list[list[float]] = []
    batch = int(config.clusters.get("batch_size", 50))
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        payload = await fetcher.request_json(
            "POST", url, headers={"Authorization": f"Bearer {key}"},
            json_body={"model": model, "input": chunk})
        data = sorted((payload or {}).get("data") or [], key=lambda d: d.get("index", 0))
        if len(data) != len(chunk):
            raise ValueError(f"embeddings: asked {len(chunk)}, got {len(data)}")
        out += [list(map(float, d["embedding"])) for d in data]
    return out


# ---------------------------------------------------------------- the job
def _today() -> date:
    return datetime.now(UTC).date()


async def _members(db: Database, config: Config, since: date) -> list[Member]:
    rows = await db.pool.fetch(
        """
        WITH latest AS (SELECT market, max(date) AS d FROM scores WHERE date > $1 GROUP BY market)
        SELECT s.topic_id, s.market, s.phase, s.money, s.competition, s.details
        FROM scores s JOIN latest l ON l.market = s.market AND l.d = s.date
        WHERE s.phase <> 'UNKNOWN'
        """, since)
    by_topic: dict[int, Member] = {}
    for r in rows:
        d = r["details"] if isinstance(r["details"], dict) else json.loads(r["details"] or "{}")
        views = float(d.get("mean30") or 0.0)
        m = by_topic.setdefault(r["topic_id"], Member(r["topic_id"], d.get("name") or "", 0.0,
                                                      False, None, 0.0))
        m.views += views
        m.markets[r["market"]] = views
        m.growing = m.growing or r["phase"] in ("BREAKOUT", "GROWING")
        if r["competition"] is not None:
            m.openness = max(m.openness or 0.0, 101.0 - r["competition"])
        m.money = max(m.money, float(r["money"] or 0))
    return list(by_topic.values())


async def _texts(db: Database, config: Config, ids: list[int]) -> dict[int, str]:
    labels = (config.monetization.get("class_labels") or {})
    rows = await db.pool.fetch("SELECT id, canonical_name, kind FROM topics WHERE id = ANY($1)",
                               ids)
    return {r["id"]: r["canonical_name"] + (f" ({labels[r['kind']]})"
                                            if r["kind"] in labels else "") for r in rows}


async def _name_with_gemini(client: httpx.AsyncClient, config: Config, secrets: Secrets,
                            names: list[str]) -> str | None:
    from ode.briefs import API

    if not secrets.gemini_api_key:
        return None
    prompt = ("These topics form one niche for a YouTube channel / website:\n"
              + "\n".join(f"- {n}" for n in names[:15])
              + "\nName the niche in 2-5 English words. Reply with the name only.")
    payload = await make_fetcher(config, client, "gemini").request_json(
        "POST", API.format(model=config.gemini.get("model", "gemini-2.5-flash")),
        headers={"x-goog-api-key": secrets.gemini_api_key},
        json_body={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                   "generationConfig": {"temperature": 0.2}})
    text = "".join(p.get("text", "") for p in payload["candidates"][0]["content"]["parts"])
    return text.strip().strip('"').splitlines()[0][:60] or None


async def cluster_job(db: Database, config: Config, secrets: Secrets,
                      client: httpx.AsyncClient) -> dict[str, Any]:
    c = config.clusters
    if not c.get("enabled", True):
        return {"status": "skipped", "reason": "clusters.enabled = false"}
    ep = _endpoint(config, secrets)
    if ep is None:
        return {"status": "skipped", "reason": "set EMBEDDINGS_URL + EMBEDDINGS_API_KEY "
                "(RunPod) or GEMINI_API_KEY"}
    url, key, model = ep
    today = _today()
    members = await _members(db, config, today - timedelta(days=14))
    if len(members) < int(c.get("min_size", 3)):
        return {"status": "skipped", "reason": "too few scored topics yet"}
    texts = await _texts(db, config, [m.topic_id for m in members])
    cached = {r["topic_id"]: r for r in await db.pool.fetch(
        "SELECT topic_id, model, text, vector FROM topic_embeddings WHERE topic_id = ANY($1)",
        list(texts))}
    todo = [tid for tid, t in texts.items()
            if tid not in cached or cached[tid]["text"] != t or cached[tid]["model"] != model]
    if todo:
        vecs = await embed(client, config, url, key, model, [texts[t] for t in todo])
        await db.pool.executemany(
            """
            INSERT INTO topic_embeddings (topic_id, model, text, vector, embedded_at)
            VALUES ($1, $2, $3, $4, now())
            ON CONFLICT (topic_id) DO UPDATE SET model = EXCLUDED.model, text = EXCLUDED.text,
                vector = EXCLUDED.vector, embedded_at = now()
            """, [(t, model, texts[t], v) for t, v in zip(todo, vecs, strict=True)])
        cached.update({t: {"topic_id": t, "model": model, "text": texts[t], "vector": v}
                       for t, v in zip(todo, vecs, strict=True)})
    members = [m for m in members if m.topic_id in cached]
    members.sort(key=lambda m: (-m.views, m.topic_id))   # strongest first
    labels = leader_cluster(np.array([cached[m.topic_id]["vector"] for m in members]),
                            float(c.get("threshold", 0.80)))
    groups: dict[int, list[Member]] = {}
    for m, lab in zip(members, labels, strict=True):
        groups.setdefault(lab, []).append(m)
    values = {code: config.market_value(code) for code in config.market_codes}
    min_size = int(c.get("min_size", 3))
    niches = [(g, niche_score(g, values)) for g in groups.values() if len(g) >= min_size]
    niches.sort(key=lambda x: -x[1]["score"])
    old = {r["signature"]: r["label"] for r in await db.pool.fetch(
        "SELECT signature, label FROM clusters WHERE signature IS NOT NULL")}
    named = 0
    rows = []
    for g, summary in niches:
        sig = signature([m.topic_id for m in g])
        label = old.get(sig)
        if label is None and named < int(c.get("max_names_per_run", 10)):
            try:
                label = await _name_with_gemini(client, config, secrets, [m.name for m in g])
                named += label is not None
            except Exception as exc:  # noqa: BLE001 - a name is a nicety
                log.debug("clusters.name_failed", error=str(exc)[:200])
        label = label or g[0].name
        detail = {**summary, "members": [{"id": m.topic_id, "name": m.name,
                                          "views": round(m.views), "growing": m.growing}
                                         for m in g[:60]]}
        rows.append((label, sig, len(g), summary["score"], json.dumps(detail, ensure_ascii=False),
                     [m.topic_id for m in g]))
    async with db.pool.acquire() as con, con.transaction():
        await con.execute("UPDATE topics SET cluster_id = NULL WHERE cluster_id IS NOT NULL")
        await con.execute("DELETE FROM clusters")
        for label, sig, size, score, detail, ids in rows:
            cid = await con.fetchval(
                "INSERT INTO clusters (label, size, coverage_score, detail, signature) "
                "VALUES ($1, $2, $3, $4::jsonb, $5) RETURNING id", label, size, score, detail, sig)
            await con.execute("UPDATE topics SET cluster_id = $1 WHERE id = ANY($2)", cid, ids)
    return {"status": "ok", "topics": len(members), "embedded": len(todo),
            "niches": len(rows), "named": named}


async def load_niches(db: Database, limit: int = 30) -> list[dict[str, Any]]:
    rows = await db.pool.fetch("SELECT id, label, size, coverage_score, detail FROM clusters "
                               "ORDER BY coverage_score DESC NULLS LAST LIMIT $1", limit)
    out = []
    for r in rows:
        d = r["detail"] if isinstance(r["detail"], dict) else json.loads(r["detail"] or "{}")
        out.append({"id": r["id"], "label": r["label"], "size": r["size"],
                    "score": r["coverage_score"], **d})
    return out


def render_niches(niches: list[dict[str, Any]]) -> str:
    if not niches:
        return "no niches yet — needs embeddings (EMBEDDINGS_URL / GEMINI_API_KEY) + `ode daily`"
    lines = []
    for i, n in enumerate(niches, 1):
        lines.append(f"{i:>2}. {n['label']} — {n['size']} topics · {n['views_per_day']:,} "
                     f"views/day · {n['growing_share']:.0%} growing · best: "
                     f"{', '.join(n['best_markets'])}")
        lines.append("     " + ", ".join(m["name"] for m in n["members"][:8]))
    return "\n".join(lines)
