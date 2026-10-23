"""Score every fresh topic in every market, persist, and queue YouTube probes.

No lookahead: every read goes through `MetricReader`, which requires an
`as_of` date and only ever selects rows with `date <= as_of`. Phase 7's
backtest reuses this exact path with historical cut-off dates.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ode.config import Config
from ode.db import Database
from ode.logging import get_logger
from ode.models import Market
from ode.scoring.components import (
    composite_score,
    confidence_score,
    dominant_share,
    gap_score,
    intent_keyword,
    log_normalize,
    money_profile,
    saturation_forecast,
    weighted_mean,
)
from ode.scoring.momentum import Momentum, MomentumParams, momentum_profile
from ode.scoring.seasonal import seasonal_peak
from ode.scoring.text import clean_query

log = get_logger(__name__)

@dataclass
class TopicScore:
    topic_id: int
    topic_key: str | None
    name: str
    market: str
    demand: float
    momentum: Momentum
    money: Any
    gap: float | None
    competition: float | None
    composite: float
    confidence: float
    saturation: str
    months_left: int | None
    details: dict[str, Any]


class MetricReader:
    """All scoring reads. `as_of` is mandatory; nothing after it is visible."""

    def __init__(self, db: Database, as_of: date) -> None:
        self.db = db
        self.as_of = as_of

    async def series(self, market: str, source: str, metric: str, days: int
                     ) -> dict[int, dict[date, float]]:
        rows = await self.db.pool.fetch(
            "SELECT topic_id, date, value FROM topic_metrics WHERE market = $1 AND source = $2 "
            "AND metric_name = $3 AND date <= $4 AND date > $5",
            market, source, metric, self.as_of, self.as_of - timedelta(days=days),
        )
        out: dict[int, dict[date, float]] = defaultdict(dict)
        for r in rows:
            out[r["topic_id"]][r["date"]] = r["value"]
        return out

    async def fresh_topics(self, market: str, within_days: int) -> list[int]:
        rows = await self.db.pool.fetch(
            "SELECT DISTINCT topic_id FROM topic_metrics WHERE market = $1 "
            "AND date <= $2 AND date > $3",
            market, self.as_of, self.as_of - timedelta(days=within_days),
        )
        return [r["topic_id"] for r in rows]

    async def topics(self, ids: list[int], market: str) -> dict[int, dict[str, Any]]:
        rows = await self.db.pool.fetch(
            """
            SELECT t.id, t.topic_key, t.canonical_name, t.kind, t.wiki_langs,
                   (SELECT w.term FROM watchlist w WHERE w.topic_key = t.topic_key
                      AND w.market = $2 ORDER BY (w.origin = 'seed') DESC LIMIT 1) AS local_title,
                   (SELECT w.origin FROM watchlist w WHERE w.topic_key = t.topic_key
                      AND w.market = $2 ORDER BY (w.origin = 'seed') DESC LIMIT 1) AS origin,
                   (SELECT a.surface_form FROM topic_aliases a WHERE a.topic_id = t.id
                      AND a.market = $2 ORDER BY a.created_at LIMIT 1) AS local_alias,
                   (SELECT array_agg(a.surface_form) FROM topic_aliases a
                      WHERE a.topic_id = t.id) AS aliases
            FROM topics t WHERE t.id = ANY($1)
            """,
            ids, market,
        )
        return {r["id"]: dict(r) for r in rows}

    async def video_channels(self, market: str, days: int = 30) -> dict[int, list[str | None]]:
        rows = await self.db.pool.fetch(
            "SELECT topic_id, channel_id FROM videos WHERE market = $1 AND topic_id IS NOT NULL "
            "AND fetched_at::date <= $2 AND fetched_at::date > $3",
            market, self.as_of, self.as_of - timedelta(days=days),
        )
        out: dict[int, list[str | None]] = defaultdict(list)
        for r in rows:
            out[r["topic_id"]].append(r["channel_id"])
        return out


def _latest(series: dict[date, float] | None) -> float | None:
    if not series:
        return None
    return series[max(series)]


class Scorer:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.s = config.scoring
        self.params = MomentumParams.from_config(self.s)
        self.markets = config.markets()
        self.weights: dict[str, float] = dict(self.s.get("composite_weights") or {})

    async def load_weights(self) -> dict[str, float]:
        """Backtest-adopted composite weights when present (and allowed), else config."""
        from ode.backtest import learned_weights

        learned = await learned_weights(self.db, self.config)
        if learned:
            self.weights = learned
        return self.weights

    async def score_market(self, market: Market, as_of: date) -> list[TopicScore]:
        rd = MetricReader(self.db, as_of)
        ids = await rd.fresh_topics(market.code, int(self.s.get("fresh_within_days", 14)))
        if not ids:
            return []
        window = self.params.window_days
        wiki = await rd.series(market.code, "wikipedia", "pageviews", max(window, 400))
        reddit = await rd.series(market.code, "reddit", "reddit_mentions", 90)
        unanswered = await rd.series(market.code, "reddit", "reddit_unanswered", 90)
        hn = await rd.series(market.code, "hackernews", "hn_mentions", 90)
        outlier = await rd.series(market.code, "youtube", "youtube_max_outlier", 30)
        total = await rd.series(market.code, "youtube", "youtube_total_results", 30)
        monthly = await rd.series(market.code, "youtube", "youtube_monthly_uploads", 400)
        channels = await rd.video_channels(market.code)
        meta = await rd.topics(ids, market.code)

        momenta = {tid: momentum_profile(wiki.get(tid, {}), as_of, self.params) for tid in ids}
        raw_demand = {
            "wikipedia": {str(t): m.mean30 for t, m in momenta.items() if m.mean30 is not None
                          and t in wiki},
            "reddit": {str(t): sum(v.values()) for t, v in reddit.items() if t in momenta},
            "hackernews": {str(t): sum(v.values()) for t, v in hn.items() if t in momenta},
        }
        norm = {src: log_normalize(vals) for src, vals in raw_demand.items()}
        comp_raw = {str(t): _latest(v) for t, v in total.items() if t in momenta}
        comp_norm = {k: 1.0 + 0.99 * v for k, v in
                     log_normalize({k: v for k, v in comp_raw.items() if v is not None}).items()}
        dweights = self.s.get("demand_weights") or {"wikipedia": 0.5, "reddit": 0.3,
                                                    "hackernews": 0.2}
        money_cfg = self.config.monetization
        configured_langs = {m.language for m in self.markets}

        out: list[TopicScore] = []
        for tid in ids:
            info = meta.get(tid)
            if info is None:
                continue
            key = str(tid)
            parts = {src: norm[src].get(key) for src in norm}
            demand = weighted_mean(parts, dweights)
            if demand is None:
                continue
            mom = momenta[tid]
            seed = self.config.seed_by_key(info["topic_key"] or "")
            name = (info["local_title"] or info["local_alias"]
                    or (info["aliases"] or [info["canonical_name"]])[0])
            texts = [name, info["canonical_name"], *(info["aliases"] or [])]
            if seed:
                texts += [seed.name, *seed.queries.values()]
            money = money_profile(texts, kind=info["kind"],
                                  audience=seed.audience if seed else None, config=money_cfg)

            ans = unanswered.get(tid) or {}
            judged = sum(reddit.get(tid, {}).get(d, 0.0) for d in ans)
            gap, gap_parts = gap_score(
                max_outlier=_latest(outlier.get(tid)),
                top_channel_share=dominant_share(channels.get(tid, [])),
                unanswered_share=(sum(ans.values()) / judged) if judged else None,
                outlier_cap=float(self.s.get("outlier_cap", 50)),
                dominant_share=float(self.s.get("dominant_channel_share", 0.30)),
            )
            competition = comp_norm.get(key)
            composite = composite_score(multiplier=mom.multiplier, demand=demand,
                                        money=money.score, gap=gap, competition=competition,
                                        weights=self.weights)
            confidence = confidence_score(
                points=mom.points, phase=mom.phase,
                demand_sources=sum(v is not None for v in parts.values()),
                has_gap=gap is not None, has_competition=competition is not None,
                money_specific=len(money.paths) > 1,
            )
            sat, months_left = saturation_forecast(monthly.get(tid, {}), as_of)
            sea_cfg = self.config.seasonal
            season = seasonal_peak(
                wiki.get(tid, {}), as_of,
                min_history_days=int(sea_cfg.get("min_history_days", 365)),
                peak_ratio=float(sea_cfg.get("peak_ratio", 2.0)),
                lead_weeks=tuple(sea_cfg.get("lead_weeks", (2, 8))))
            langs = set(info["wiki_langs"] or [])
            details = {
                "name": name,
                "origin": info["origin"] or ("seed" if seed else None),
                "momentum": mom.reason,
                "accel_t": None if mom.accel_t is None else round(mom.accel_t, 2),
                "mean30": None if mom.mean30 is None else round(mom.mean30, 1),
                "change30": None if mom.change30 is None else round(mom.change30, 3),
                "points": mom.points,
                "demand_parts": {k: round(v, 1) for k, v in parts.items() if v is not None},
                "money_paths": money.paths,
                "tutorial": money.tutorial,
                "tool_keyword": intent_keyword(texts, money_cfg.get("tool_keywords") or []),
                "comparison_penalty": money.comparison_penalty,
                "gap_parts": {k: round(v, 1) for k, v in gap_parts.items()},
                "competing_videos_180d": comp_raw.get(key),
                "wiki_langs": len(langs) or None,
                # other configured languages with no article (this market obviously has data)
                "missing_in": (sorted(configured_langs - langs - {market.language})
                               if langs else []),
                "months_left": months_left,
                "seasonal": season.as_dict() if season else None,
            }
            out.append(TopicScore(tid, info["topic_key"], name, market.code, round(demand, 2),
                                  mom, money, None if gap is None else round(gap, 2),
                                  None if competition is None else round(competition, 2),
                                  composite, confidence, sat, months_left, details))
        out.sort(key=lambda t: t.composite, reverse=True)
        return out

    async def persist(self, market: Market, as_of: date, scored: list[TopicScore]) -> None:
        async with self.db.pool.acquire() as con, con.transaction():
            await con.execute("DELETE FROM scores WHERE market = $1 AND date = $2",
                              market.code, as_of)
            await con.executemany(
                """
                INSERT INTO scores (topic_id, market, date, demand, momentum, money, gap,
                    competition, saturation_risk, composite, phase, confidence, velocity,
                    acceleration, spike_ratio, money_path, details)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
                """,
                [(t.topic_id, market.code, as_of, t.demand, t.momentum.multiplier,
                  t.money.score, t.gap, t.competition, t.saturation, t.composite,
                  t.momentum.phase, t.confidence, t.momentum.velocity,
                  t.momentum.acceleration, t.momentum.spike_ratio, t.money.primary,
                  json.dumps(t.details, default=str)) for t in scored],
            )
            await con.executemany(
                """
                INSERT INTO monetization (topic_id, primary_path, est_rpv, paths, updated_at)
                VALUES ($1, $2, $3, $4, now())
                ON CONFLICT (topic_id) DO UPDATE SET primary_path = EXCLUDED.primary_path,
                    est_rpv = EXCLUDED.est_rpv, paths = EXCLUDED.paths, updated_at = now()
                """,
                [(t.topic_id, t.money.primary, t.money.est_rpv, json.dumps(t.money.paths))
                 for t in scored],
            )

    async def enqueue_probes(self, market: Market, scored: list[TopicScore], as_of: date) -> int:
        """Queue the best topics that lack fresh YouTube data for a YouTube check."""
        k = int(self.s.get("probe_top_k", 8))
        refresh = int((self.config.youtube or {}).get("probe_refresh_days", 30))
        if k <= 0:
            return 0
        recent = await self.db.pool.fetch(
            "SELECT DISTINCT t.topic_key FROM topic_metrics tm JOIN topics t ON t.id = tm.topic_id "
            "WHERE tm.market = $1 AND tm.source = 'youtube' AND tm.date > $2",
            market.code, as_of - timedelta(days=refresh),
        )
        have_youtube = {r["topic_key"] for r in recent}
        picked = [t for t in scored
                  if t.topic_key and not t.topic_key.startswith("seed:")
                  and t.topic_key not in have_youtube and t.momentum.phase != "UNKNOWN"][:k]
        queued = 0
        for t in picked:
            status = await self.db.pool.execute(
                """
                INSERT INTO probe_queue (market, topic_key, query, reason, score)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (market, topic_key) DO UPDATE SET
                    probed_at = NULL, queued_at = now(), query = EXCLUDED.query,
                    reason = EXCLUDED.reason, score = EXCLUDED.score
                WHERE probe_queue.probed_at IS NOT NULL
                  AND probe_queue.probed_at < now() - make_interval(days => $6)
                """,
                market.code, t.topic_key, clean_query(t.name),
                f"{t.momentum.phase} composite {t.composite:.1f}", t.composite, refresh,
            )
            queued += status.endswith(" 1")
        return queued

    async def run(self, as_of: date, market_codes: list[str] | None = None) -> list[dict]:
        reports = []
        await self.load_weights()
        for market in self.markets:
            if market_codes and market.code not in market_codes:
                continue
            run_id = await self.db.start_run("scoring", market.code)
            try:
                scored = await self.score_market(market, as_of)
                await self.persist(market, as_of, scored)
                probes = await self.enqueue_probes(market, scored, as_of)
            except Exception as exc:
                log.exception("scoring.failed", market=market.code, error=str(exc)[:300])
                await self.db.finish_run(run_id, status="error", errors=1,
                                         detail={"error": str(exc)[:500]})
                reports.append({"market": market.code, "status": "error", "error": str(exc)})
                continue
            phases: dict[str, int] = defaultdict(int)
            for t in scored:
                phases[t.momentum.phase] += 1
            rep = {"market": market.code, "status": "ok", "scored": len(scored),
                   "phases": dict(phases), "probes_queued": probes, "as_of": str(as_of)}
            await self.db.finish_run(run_id, status="ok", items=len(scored), detail=rep)
            log.info("scoring.done", **rep)
            reports.append(rep)
        return reports


__all__ = ["MetricReader", "Scorer", "TopicScore", "clean_query"]
