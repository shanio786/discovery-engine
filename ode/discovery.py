"""Discovery: decide WHAT to track, so the engine finds topics nobody told it about.

Per run:
  1. Seeds   — resolve each seed's English article to its Wikidata entity and
               that entity's title in every configured language. Seeds are
               always tracked and never evicted.
  2. Find    — per market: the language's daily top-1000 articles (mainstream
               breakouts) + Wikipedia "morelike" neighbours of every seed
               (the niche, tutorial-heavy neighbourhood).
  3. Filter  — cheap title rules, then non-articles, then Wikidata classes that
               are never content opportunities (people, films, sports, ...).
  4. Expand  — every kept topic is also tracked in every other market under
               that language's title (cross-language arbitrage backbone).
  5. Evict   — stale items and anything beyond the per-market cap go inactive.

Everything is recorded in collector_runs as collector='discovery'.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from ode.collectors.base import make_fetcher
from ode.config import Config
from ode.db import Database
from ode.http import CircuitOpenError, UpstreamError
from ode.logging import get_logger
from ode.models import Market
from ode.ratelimit import RetryableHTTPError
from ode.wikimeta import EntityInfo, PageInfo, TitleFilter, WikiMeta, exclusion_reason, norm_title

log = get_logger(__name__)

KIND = "wiki_article"


@dataclass
class MarketReport:
    market: str
    candidates: int = 0
    new: int = 0
    excluded: int = 0
    langlinks: int = 0
    evicted: dict[str, int] = field(default_factory=dict)
    days_scanned: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def plan_langlinks(
    entity: EntityInfo, markets: list[Market], skip_market: str
) -> list[tuple[Market, str]]:
    """Pure: (market, title) pairs to track this entity in every other market."""
    out: list[tuple[Market, str]] = []
    for m in markets:
        if m.code == skip_market:
            continue
        title = entity.sitelinks.get(m.wiki_site)
        if title:
            out.append((m, norm_title(title)))
    return out


class Discoverer:
    def __init__(self, config: Config, client: httpx.AsyncClient, db: Database) -> None:
        self.config = config
        self.db = db
        self.meta = WikiMeta(make_fetcher(config, client, "wikipedia"),
                             make_fetcher(config, client, "wikidata"))
        d = config.discovery
        self.filter = TitleFilter(d.get("exclude_titles", []), d.get("exclude_title_patterns", []))
        self.excluded_classes: dict[str, str] = {
            str(k): str(v) for k, v in (d.get("exclude_wikidata_classes") or {}).items()
        }
        self.today = datetime.now(UTC).date()

    # ------------------------------------------------------------------ seeds
    async def sync_seeds(self, markets: list[Market]) -> dict[str, dict[str, str]]:
        """Track every seed in every market it has an article in.
        Returns {market_code: {seed_key: local title}} for morelike expansion."""
        titles_by_market: dict[str, dict[str, str]] = {m.code: {} for m in markets}
        wiki_seeds = [s for s in self.config.seeds if s.wiki]
        for seed in self.config.seeds:
            if not seed.wiki:
                await self.db.ensure_seed_topic(seed)
        if not wiki_seeds:
            return titles_by_market

        pages = await self.meta.resolve_titles("en.wikipedia.org", [s.wiki for s in wiki_seeds])
        qids = [p.qid for p in pages.values() if p.qid]
        entities = await self.meta.entities(qids) if qids else {}

        for seed in wiki_seeds:
            page = pages.get(seed.wiki)
            if page is None or page.missing or page.ns != 0:
                log.warning("discovery.seed_unresolved", seed=seed.key, wiki=seed.wiki,
                            hint="fix the `wiki:` title in config.yaml")
                await self.db.ensure_seed_topic(seed)
                continue
            entity = entities.get(page.qid) if page.qid else None
            key = await self.db.ensure_seed_topic(
                seed, page.qid, entity.wiki_langs if entity else None
            )
            for m in markets:
                title = page.title if m.language == "en" else (
                    entity.sitelinks.get(m.wiki_site) if entity else None
                )
                if not title:
                    continue
                title = norm_title(title)
                titles_by_market[m.code][seed.key] = title
                await self.db.watchlist_upsert(m.code, KIND, title, key, "seed", today=self.today)
        return titles_by_market

    # ------------------------------------------------------------------ find
    async def _safe(self, report: MarketReport, what: str, coro):
        """Await one upstream call; record expected upstream failures and go on.
        CircuitOpenError still propagates (the host is down: stop this market)."""
        try:
            return await coro
        except (UpstreamError, RetryableHTTPError) as exc:
            report.errors.append(f"{what}: {exc}"[:300])
            return None

    async def _top_candidates(self, market: Market, days_back: int, report: MarketReport) -> set[str]:
        found: set[str] = set()
        for i in range(1, days_back + 1):  # yesterday backwards; today is never published
            day = self.today - timedelta(days=i)
            arts = await self._safe(report, f"top {day}",
                                    self.meta.top_articles(market.wiki_project, day))
            if arts is None:
                continue
            report.days_scanned += 1
            found.update(title for title, _ in arts)
        return found

    async def _morelike_candidates(
        self, market: Market, seed_titles: dict[str, str], report: MarketReport
    ) -> set[str]:
        limit = int(self.config.discovery.get("morelike_per_seed", 25))
        found: set[str] = set()
        for title in seed_titles.values():
            hits = await self._safe(report, f"morelike {title}",
                                    self.meta.morelike(market.wiki_host, title, limit))
            found.update(hits or [])
        return found

    # ------------------------------------------------------------------ run
    async def discover_market(
        self, market: Market, markets: list[Market], seed_titles: dict[str, str]
    ) -> MarketReport:
        report = MarketReport(market.code)
        existing = await self.db.watchlist_rows(market.code, KIND)
        has_top = any(r["origin"] == "wiki_top" for r in existing.values())
        d = self.config.discovery
        days_back = int(d.get("top_days_back", 3) if has_top else d.get("bootstrap_days_back", 14))

        origins: dict[str, str] = {}
        for t in await self._top_candidates(market, days_back, report):
            origins.setdefault(t, "wiki_top")
        for t in await self._morelike_candidates(market, seed_titles, report):
            origins[t] = "morelike"  # niche neighbourhood wins the label

        candidates = {t: o for t, o in origins.items() if not self.filter.reason(t)}
        report.candidates = len(candidates)

        # Already-known titles: just refresh (revives stale/evicted rows).
        for title, origin in candidates.items():
            row = existing.get(title)
            if row is not None:
                await self.db.watchlist_upsert(market.code, KIND, title, row["topic_key"],
                                               origin, today=self.today)
                if row["topic_key"]:
                    await self.db.watchlist_touch_topic(row["topic_key"], self.today)

        new_titles = [t for t in candidates if t not in existing]
        if new_titles:
            pages = await self.meta.resolve_titles(market.wiki_host, new_titles)
            qids = sorted({p.qid for p in pages.values() if p.qid})
            entities = await self.meta.entities(qids) if qids else {}
            for title in new_titles:
                await self._admit(market, markets, title, candidates[title],
                                  pages.get(title), entities, report)

        report.evicted = await self.db.watchlist_evict(
            market.code, KIND,
            cap=int(d.get("max_tracked_per_market", 2500)),
            stale_after_days=int(d.get("stale_after_days", 45)),
            today=self.today,
        )
        return report

    async def _admit(
        self,
        market: Market,
        markets: list[Market],
        title: str,
        origin: str,
        page: PageInfo | None,
        entities: dict[str, EntityInfo],
        report: MarketReport,
    ) -> None:
        if page is None or page.missing or page.ns != 0:
            # Remember it so we don't re-resolve it every day.
            await self.db.watchlist_upsert(market.code, KIND, title, None, origin, active=False,
                                           excluded_reason="not an article", today=self.today)
            report.excluded += 1
            return
        entity = entities.get(page.qid) if page.qid else None
        reason = exclusion_reason(entity, self.excluded_classes)
        if page.qid:
            key = await self.db.ensure_topic_for_qid(
                page.qid,
                name=(entity.label_en if entity and entity.label_en else page.title),
                wiki_langs=entity.wiki_langs if entity else None,
                kind=(entity.p31[0] if entity and entity.p31 else None),
            )
        else:
            key = f"wp:{market.wiki_project}:{page.title}"
        canonical = norm_title(page.title)
        added = await self.db.watchlist_upsert(
            market.code, KIND, canonical, key, origin,
            active=reason is None, excluded_reason=reason, today=self.today,
        )
        if reason:
            report.excluded += 1
            return
        if added:
            report.new += 1
        if entity:
            for other, other_title in plan_langlinks(entity, markets, market.code):
                if await self.db.watchlist_upsert(other.code, KIND, other_title, key, "langlink",
                                                  today=self.today):
                    report.langlinks += 1

    async def admit_titles(self, market: Market, titles: list[str], origin: str, *,
                           include_people: bool = False) -> list[dict]:
        """Resolve + filter + admit titles found by realtime sources (hourly
        spikes, Google Trends). Same rules as discovery. Returns one dict per
        input: {title, canonical, topic_key, active, reason}."""
        report = MarketReport(market.code)
        wanted = [t for t in dict.fromkeys(norm_title(t) for t in titles if t)
                  if not self.filter.reason(t)]
        if not wanted:
            return []
        pages = await self.meta.resolve_titles(market.wiki_host, wanted)
        qids = sorted({p.qid for p in pages.values() if p.qid})
        entities = await self.meta.entities(qids) if qids else {}
        saved = self.excluded_classes
        if include_people:
            self.excluded_classes = {k: v for k, v in saved.items() if k != "Q5"}
        try:
            for title in wanted:
                await self._admit(market, self.config.markets(), title, origin,
                                  pages.get(title), entities, report)
        finally:
            self.excluded_classes = saved
        rows = await self.db.watchlist_rows(market.code, KIND)
        out = []
        for title in wanted:
            page = pages.get(title)
            canonical = norm_title(page.title) if page else title
            row = rows.get(canonical) or {}
            out.append({"title": title, "canonical": canonical,
                        "topic_key": row.get("topic_key"), "active": bool(row.get("active")),
                        "reason": row.get("excluded_reason")})
        return out

    async def run(self, market_codes: list[str] | None = None) -> list[dict]:
        markets = self.config.markets()
        targets = [m for m in markets if not market_codes or m.code in market_codes]
        await self.db.close_orphan_runs()

        seed_run = await self.db.start_run("discovery", "seeds")
        try:
            seed_titles = await self.sync_seeds(markets)
            n = sum(len(v) for v in seed_titles.values())
            await self.db.finish_run(seed_run, status="ok", items=n)
        except Exception as exc:  # noqa: BLE001 - seeds failing must not stop discovery
            log.error("discovery.seeds_failed", error=str(exc)[:300])
            await self.db.finish_run(seed_run, status="error", errors=1,
                                     detail={"error": str(exc)[:500]})
            seed_titles = {m.code: {} for m in markets}

        reports: list[dict] = []
        for market in targets:
            run_id = await self.db.start_run("discovery", market.code)
            try:
                rep = await self.discover_market(market, markets, seed_titles.get(market.code, {}))
                if not rep.errors:
                    status = "ok"
                else:
                    status = "partial" if (rep.candidates or rep.days_scanned) else "error"
            except (CircuitOpenError, UpstreamError, RetryableHTTPError) as exc:
                log.error("discovery.market_failed", market=market.code, error=str(exc)[:300])
                rep = MarketReport(market.code, errors=[str(exc)[:500]])
                status = "error"
            except Exception as exc:
                log.exception("discovery.market_failed", market=market.code,
                              error=str(exc)[:300])
                rep = MarketReport(market.code, errors=[str(exc)[:500]])
                status = "error"
            await self.db.finish_run(run_id, status=status, items=rep.new + rep.langlinks,
                                     errors=len(rep.errors), detail=rep.as_dict())
            log.info("discovery.market_done", **rep.as_dict())
            reports.append({**rep.as_dict(), "status": status})
        return reports
