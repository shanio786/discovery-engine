"""Reddit collector (application-only OAuth).

Per market, per seed query: daily post volume (`reddit_mentions`) and daily
count of posts nobody answered (`reddit_unanswered`) — the pain-point signal.

Correctness rules:
  * Each market searches its OWN scope (en-US: site-wide English; others:
    their national/language subreddits) so markets never share one signal.
  * Results are paged newest -> oldest. A day is only written when it is
    fully covered by the pages we fetched; a partially-seen oldest day would
    otherwise overwrite a correct count with a smaller one.
  * "Unanswered" is only judged for posts older than `unanswered_min_age_hours`.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ode.collectors.base import BaseCollector
from ode.models import Market, RawSignal, Seed
from ode.state import day_range, incremental_start

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"


def search_url(scope: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Endpoint + extra params for a market scope."""
    subs = scope.get("subreddits") or []
    if subs:
        return f"{API}/r/{'+'.join(subs)}/search", {"restrict_sr": "1"}
    return f"{API}/search", {}


def parse_posts(payload: Any) -> tuple[list[dict[str, Any]], str | None]:
    """Listing -> ([{id, created, num_comments}], after cursor)."""
    if not isinstance(payload, dict):
        return [], None
    data = payload.get("data") or {}
    posts: list[dict[str, Any]] = []
    for child in data.get("children") or []:
        d = child.get("data") or {}
        try:
            posts.append({
                "id": d.get("name") or d.get("id"),
                "created": datetime.fromtimestamp(float(d["created_utc"]), tz=UTC),
                "num_comments": int(d.get("num_comments") or 0),
            })
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            continue
    return posts, data.get("after")


def aggregate_reddit(
    posts: list[dict[str, Any]],
    *,
    market: str,
    query: str,
    topic_key: str | None,
    start: date,
    exhausted: bool,
    now: datetime,
    min_age_hours: int = 48,
) -> list[RawSignal]:
    """Pure: posts (any order) -> complete-day mention/unanswered signals.

    `exhausted` = the listing ended (no more pages): every day from `start` is
    fully covered. Otherwise coverage begins the day AFTER the oldest post seen
    — unless we already paged past `start`.
    """
    unique = {p["id"]: p for p in posts if p.get("id")}.values()
    today = now.date()
    if unique:
        oldest = min(p["created"].date() for p in unique)
    else:
        oldest = None
    if exhausted or (oldest is not None and oldest < start):
        covered_from = start
    elif oldest is None:
        return []  # nothing fetched and listing not exhausted: no knowledge
    else:
        covered_from = max(start, oldest + timedelta(days=1))

    mentions: dict[date, int] = defaultdict(int)
    unanswered: dict[date, int] = defaultdict(int)
    for p in unique:
        d = p["created"].date()
        mentions[d] += 1
        if p["num_comments"] == 0:
            unanswered[d] += 1

    # every post on a day before this cutoff is at least min_age_hours old
    judge_before = (now - timedelta(hours=min_age_hours)).date()
    out: list[RawSignal] = []
    for d in day_range(covered_from, today):
        out.append(RawSignal("reddit", market, query, "reddit_mentions", float(mentions[d]), d,
                             raw={"query": query}, topic_key=topic_key))
        if d < judge_before:
            out.append(RawSignal("reddit", market, query, "reddit_unanswered",
                                 float(unanswered[d]), d, raw={"query": query},
                                 topic_key=topic_key))
    return out


class RedditCollector(BaseCollector):
    name = "reddit"
    cost = "free"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._token: str | None = None

    def is_enabled(self) -> tuple[bool, str]:
        if not (self.secrets.reddit_client_id and self.secrets.reddit_client_secret):
            return False, "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set"
        return True, ""

    def supports(self, market: Market) -> tuple[bool, str]:
        if not self.config.reddit_scope(market):
            return False, f"no reddit scope configured for {market.code}"
        if not self.config.queries_for(market):
            return False, f"no seed queries in language '{market.language}'"
        return True, ""

    async def _get_token(self) -> str:
        if self._token:
            return self._token
        payload = await self.http.request_json(
            "POST", TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self.secrets.reddit_client_id or "", self.secrets.reddit_client_secret or ""),
            headers={"User-Agent": self.secrets.reddit_user_agent},
        )
        token = (payload or {}).get("access_token")
        if not token:
            raise RuntimeError("reddit token response had no access_token")
        self._token = token
        return token

    async def collect(self, market: Market, since: datetime) -> list[RawSignal]:
        scope = self.config.reddit_scope(market) or {}
        url, extra = search_url(scope)
        max_pages = int(self.config.reddit.get("max_pages", 5))
        min_age = int(self.config.reddit.get("unanswered_min_age_hours", 48))
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}", "User-Agent": self.secrets.reddit_user_agent}
        last = await self.state.last_dates(market.code, self.name, "reddit_mentions")
        lookback_start = since.date()
        overlap = self.config.overlap_days(self.name)

        async def one(pair: tuple[Seed, str]) -> list[RawSignal]:
            seed, query = pair
            start = incremental_start(
                last.get(seed.topic_key), lookback_start=lookback_start, overlap_days=overlap
            )
            posts: list[dict[str, Any]] = []
            after: str | None = None
            exhausted = False
            for _ in range(max_pages):
                params = {"q": query, "sort": "new", "limit": "100", "t": "year", **extra}
                if after:
                    params["after"] = after
                page, after = parse_posts(await self.http.get_json(url, params=params,
                                                                   headers=headers))
                posts.extend(page)
                if not after or not page:
                    exhausted = True
                    break
                if min(p["created"].date() for p in page) < start:
                    break
            return aggregate_reddit(posts, market=market.code, query=query,
                                    topic_key=seed.topic_key, start=start, exhausted=exhausted,
                                    now=datetime.now(UTC), min_age_hours=min_age)

        batches = await self.map_items(self.config.queries_for(market), one, label="query")
        signals = [s for b in batches for s in b]
        self.log.info("reddit.collected", market=market.code, signals=len(signals),
                      errors=self.errors)
        return signals
