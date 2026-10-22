"""Wikipedia Pageviews collector — the demand backbone.

Free, unlimited, multi-year daily history in every language. No auth (a
descriptive User-Agent is required by Wikimedia policy and set on the client).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote

from ode.collectors.base import BaseCollector
from ode.models import Market, RawSignal

REST_BASE = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"


def _parse_ts(ts: str) -> date:
    # timestamps look like "2024010100" (YYYYMMDDHH)
    return datetime.strptime(ts[:8], "%Y%m%d").date()


def parse_pageviews(payload: Any, market: str, article: str) -> list[RawSignal]:
    """Pure parser: Wikimedia JSON -> daily pageview signals. Testable offline."""
    if not payload:
        return []
    out: list[RawSignal] = []
    for item in payload.get("items", []):
        try:
            observed = _parse_ts(item["timestamp"])
            views = float(item["views"])
        except (KeyError, ValueError, TypeError):
            continue
        out.append(
            RawSignal(
                source="wikipedia",
                market=market,
                topic_surface_form=article.replace("_", " "),
                metric_name="pageviews",
                value=views,
                observed_at=observed,
                raw={"project": item.get("project"), "article": article},
            )
        )
    return out


class WikipediaCollector(BaseCollector):
    name = "wikipedia"
    cost = "free"

    async def collect(self, market: Market, since: datetime) -> list[RawSignal]:
        start = since.strftime("%Y%m%d")
        end = datetime.now(UTC).strftime("%Y%m%d")
        signals: list[RawSignal] = []
        for article in self.config.seed_wiki_articles:
            url = (
                f"{REST_BASE}/{market.wiki_project}/all-access/all-agents/"
                f"{quote(article, safe='')}/daily/{start}/{end}"
            )
            payload = await self._get_json(url)
            if payload is None:
                # article doesn't exist in this language — expected, skip quietly
                self.log.debug("wiki.article_404", article=article, project=market.wiki_project)
                continue
            parsed = parse_pageviews(payload, market.code, article)
            signals.extend(parsed)
            self.log.debug("wiki.article_ok", article=article, days=len(parsed))
        return signals
