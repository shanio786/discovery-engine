"""Reddit collector (application-only OAuth).

Signal = per-day volume of posts matching a seed term, plus how many are
UNANSWERED (num_comments == 0) — the pain-point signal. Self-skips when no
credentials are configured.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Any

from ode.collectors.base import BaseCollector
from ode.models import Market, RawSignal
from ode.ratelimit import RetryableHTTPError

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
SEARCH_URL = "https://oauth.reddit.com/search"


def parse_reddit(payload: Any, market: str, query: str) -> list[RawSignal]:
    """Pure parser: Reddit listing -> per-day mention + unanswered counts."""
    if not payload:
        return []
    children = payload.get("data", {}).get("children", [])
    mentions: dict[date, int] = defaultdict(int)
    unanswered: dict[date, int] = defaultdict(int)
    for child in children:
        data = child.get("data", {})
        created = data.get("created_utc")
        if created is None:
            continue
        try:
            d = datetime.fromtimestamp(float(created), tz=UTC).date()
        except (ValueError, OSError, OverflowError):
            continue
        mentions[d] += 1
        if int(data.get("num_comments", 0)) == 0:
            unanswered[d] += 1

    signals: list[RawSignal] = []
    for d, count in sorted(mentions.items()):
        signals.append(
            RawSignal("reddit", market, query, "reddit_mentions", float(count), d,
                      raw={"query": query})
        )
    for d, count in sorted(unanswered.items()):
        signals.append(
            RawSignal("reddit", market, query, "reddit_unanswered", float(count), d,
                      raw={"query": query})
        )
    return signals


class RedditCollector(BaseCollector):
    name = "reddit"
    cost = "free"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._token: str | None = None

    def is_enabled(self) -> tuple[bool, str]:
        if not (self.secrets.reddit_client_id and self.secrets.reddit_client_secret):
            return False, "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set"
        return True, ""

    async def _get_token(self) -> str:
        if self._token:
            return self._token
        await self.bucket.acquire()
        resp = await self.client.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self.secrets.reddit_client_id, self.secrets.reddit_client_secret),
            headers={"User-Agent": self.secrets.reddit_user_agent},
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            raise RetryableHTTPError(f"reddit token {resp.status_code}")
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    async def collect(self, market: Market, since: datetime) -> list[RawSignal]:
        token = await self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": self.secrets.reddit_user_agent,
        }
        signals: list[RawSignal] = []
        for query in self.config.seed_queries:
            payload = await self._get_json(
                SEARCH_URL,
                params={"q": query, "sort": "new", "limit": 100, "t": "year"},
                headers=headers,
            )
            parsed = parse_reddit(payload, market.code, query)
            signals.extend(parsed)
            self.log.debug("reddit.query_ok", query=query, signals=len(parsed))
        return signals
