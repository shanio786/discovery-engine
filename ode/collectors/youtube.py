"""YouTube Data API v3 collector.

Per market, within the persisted daily quota, in priority order:
  1. seeds that are due (outlier videos, channel stats, competing-video count),
  2. probes: the scorer's best discovered topics that have no YouTube data yet,
  3. a one-off backfill of monthly upload counts (the saturation forecast's
     new-entrant signal; each completed month is fetched once, ever). Enforces the persisted daily quota: it
reserves units BEFORE every call and stops cleanly if the cap would break,
rather than failing mid-run. Self-skips when no API key is set.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ode.collectors.base import BaseCollector
from ode.http import CircuitOpenError
from ode.models import Market, RawSignal, Seed
from ode.scoring.components import completed_months

SEARCH = "https://www.googleapis.com/youtube/v3/search"
VIDEOS = "https://www.googleapis.com/youtube/v3/videos"
CHANNELS = "https://www.googleapis.com/youtube/v3/channels"

OUTLIER_SUB_CEILING = 100_000  # only channels under this count as "outliers"
_DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def iso8601_duration_to_seconds(value: str | None) -> int | None:
    """'PT12M30S' -> 750. Returns None for unparseable / non-standard values."""
    if not value:
        return None
    m = _DURATION_RE.fullmatch(value)
    if not m or value == "PT":
        return None
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def parse_total_results(payload: Any) -> int | None:
    """search.list pageInfo.totalResults (approximate count of matching videos)."""
    if not isinstance(payload, dict):
        return None
    n = (payload.get("pageInfo") or {}).get("totalResults")
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        return None
    return n


def build_signals(
    videos: dict[str, dict[str, Any]],
    channels: dict[str, dict[str, Any]],
    *,
    market: str,
    query: str,
    observed: Any,
    topic_key: str | None = None,
) -> list[RawSignal]:
    """Pure builder: video + channel stats -> entity rows + aggregate metrics.

    Emits one entity-only signal per video (stores the video + channel), plus
    per-query daily snapshot metrics: max outlier ratio and video count.
    """
    out: list[RawSignal] = []
    ratios: list[float] = []
    for vid_id, v in videos.items():
        ch_id = v.get("channel_id")
        ch = channels.get(ch_id, {}) if ch_id else {}
        subs = ch.get("subs")
        views = v.get("views")
        ratio: float | None = None
        if subs and views is not None and 0 < subs < OUTLIER_SUB_CEILING:
            ratio = float(views) / float(subs)
            ratios.append(ratio)
        out.append(
            RawSignal(
                source="youtube",
                market=market,
                topic_surface_form=query,
                metric_name="",  # entity-only; no time-series row
                value=0.0,
                observed_at=observed,
                video={
                    "id": vid_id,
                    "channel_id": ch_id if ch else None,
                    "title": v.get("title"),
                    "views": views,
                    "published_at": v.get("published_at"),
                    "duration": v.get("duration"),
                    "market": market,
                },
                channel={
                    "id": ch_id,
                    "title": ch.get("title"),
                    "subs": subs,
                    "total_views": ch.get("total_views"),
                    "country": ch.get("country"),
                    "created_at": ch.get("created_at"),
                } if ch else None,
                raw={"outlier_ratio": ratio},
                topic_key=topic_key,
            )
        )
    if ratios:
        out.append(RawSignal("youtube", market, query, "youtube_max_outlier", max(ratios),
                             observed, raw={"query": query}, topic_key=topic_key))
    out.append(RawSignal("youtube", market, query, "youtube_video_count", float(len(videos)),
                         observed, raw={"query": query}, topic_key=topic_key))
    return out


class YouTubeCollector(BaseCollector):
    name = "youtube"
    cost = "free"  # free tier of a paid-capable API; quota-limited

    def is_enabled(self) -> tuple[bool, str]:
        if not self.secrets.youtube_api_key:
            return False, "YOUTUBE_API_KEY not set"
        return True, ""

    async def _ordered_queries(self, market: Market) -> list[tuple[Seed, str]]:
        """Least-recently-searched first, so a quota-starved day still rotates
        through every seed over a few days instead of always the same ones."""
        last = await self.state.last_dates(market.code, self.name, "youtube_video_count")
        pairs = self.config.queries_for(market)
        return sorted(pairs, key=lambda p: (last.get(p[0].topic_key) or date.min, p[0].key))

    def _base_params(self, market: Market, query: str) -> dict[str, Any]:
        return {"key": self.secrets.youtube_api_key, "q": query, "part": "id", "type": "video",
                "regionCode": market.region, "relevanceLanguage": market.language}

    async def _search_topic(self, market: Market, topic_key: str, query: str,
                            published_after: str, observed: date) -> list[RawSignal] | None:
        """One topic: search + video stats + channel stats. None = quota exhausted."""
        cost = self.config.youtube_unit_cost
        if not await self.charge_quota(cost["search"]):
            self.notes.append(f"daily quota reached before '{query}'")
            return None
        search = await self.http.get_json(SEARCH, params={
            **self._base_params(market, query), "order": "viewCount",
            "maxResults": int(self.config.youtube.get("max_results", 25)),
            "publishedAfter": published_after,
        })
        video_ids = [it["id"]["videoId"] for it in (search or {}).get("items", [])
                     if (it.get("id") or {}).get("videoId")]
        videos: dict[str, dict[str, Any]] = {}
        channels: dict[str, dict[str, Any]] = {}
        if video_ids:
            if not await self.charge_quota(cost["videos"]):
                self.notes.append("daily quota reached at videos.list")
                return None
            videos = self._parse_videos(await self.http.get_json(VIDEOS, params={
                "key": self.secrets.youtube_api_key, "part": "statistics,snippet,contentDetails",
                "id": ",".join(video_ids)}))
            channel_ids = sorted({v["channel_id"] for v in videos.values() if v.get("channel_id")})
            if channel_ids:
                if not await self.charge_quota(cost["channels"]):
                    self.notes.append("daily quota reached at channels.list")
                    return None
                channels = self._parse_channels(await self.http.get_json(CHANNELS, params={
                    "key": self.secrets.youtube_api_key, "part": "statistics,snippet",
                    "id": ",".join(channel_ids)}))
        signals = build_signals(videos, channels, market=market.code, query=query,
                                observed=observed, topic_key=topic_key)
        total = parse_total_results(search)
        if total is not None:  # competing videos published in the lookback window
            signals.append(RawSignal("youtube", market.code, query, "youtube_total_results",
                                     float(total), observed, topic_key=topic_key))
        return signals

    async def _month_uploads(self, market: Market, topic_key: str, query: str,
                             month: date) -> list[RawSignal] | None:
        """Videos published in one completed month (new-entrant proxy). None = quota."""
        if not await self.charge_quota(self.config.youtube_unit_cost["search"]):
            self.notes.append("daily quota reached during monthly backfill")
            return None
        nxt = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
        payload = await self.http.get_json(SEARCH, params={
            **self._base_params(market, query), "maxResults": 0,
            "publishedAfter": f"{month:%Y-%m-%d}T00:00:00Z",
            "publishedBefore": f"{nxt:%Y-%m-%d}T00:00:00Z",
        })
        total = parse_total_results(payload)
        if total is None:
            raise ValueError(f"no totalResults for '{query}' {month:%Y-%m}")
        return [RawSignal("youtube", market.code, query, "youtube_monthly_uploads", float(total),
                          month, topic_key=topic_key)]

    async def _guarded(self, label: str, coro) -> tuple[list[RawSignal], bool]:
        """Run one unit of work. Returns (signals, keep_going)."""
        try:
            got = await coro
        except CircuitOpenError as exc:
            self.aborted = str(exc)
            return [], False
        except Exception as exc:  # noqa: BLE001 - one query must not kill the run
            self.errors += 1
            self.log.warning("youtube.item_failed", item=label, error=str(exc)[:300])
            return [], True
        if got is None:  # quota exhausted
            return [], False
        return got, True

    async def collect(self, market: Market, since: datetime) -> list[RawSignal]:
        yt = self.config.youtube
        today = datetime.now(UTC).date()
        published_after = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        signals: list[RawSignal] = []
        if (not self.config.queries_for(market)
                and not await self.state.pending_probes(market.code, 1)
                and not await self.state.probed(market.code)):
            self.skip_reason = (f"nothing to search in {market.code} yet: no seed queries in "
                                f"'{market.language}' and no probes queued by the scorer")
            return []

        # 1) seeds that are due (least recently searched first)
        last = await self.state.last_dates(market.code, self.name, "youtube_video_count")
        refresh = timedelta(days=int(yt.get("seed_refresh_days", 3)))
        go = True
        for seed, query in await self._ordered_queries(market):
            seen = last.get(seed.topic_key)
            if seen is not None and today - seen < refresh:
                continue
            got, go = await self._guarded(query, self._search_topic(
                market, seed.topic_key, query, published_after, today))
            signals.extend(got)
            if not go:
                break

        # 2) probes: the scorer's best discovered topics without YouTube data
        if go:
            for probe in await self.state.pending_probes(
                    market.code, int(yt.get("max_probes_per_market", 8))):
                got, go = await self._guarded(probe.query, self._search_topic(
                    market, probe.topic_key, probe.query, published_after, today))
                if got:
                    signals.extend(got)
                    await self.state.mark_probed(market.code, probe.topic_key)
                if not go:
                    break

        # 3) monthly-upload backfill for saturation (each past month fetched once)
        if go:
            targets: dict[str, str] = {s.topic_key: q for s, q in self.config.queries_for(market)}
            for probe in await self.state.probed(market.code):
                targets.setdefault(probe.topic_key, probe.query)
            have = await self.state.observed_dates(market.code, self.name,
                                                   "youtube_monthly_uploads")
            months = completed_months(today, int(yt.get("saturation_months", 12)))
            budget = int(yt.get("max_month_fills", 40))
            for key, query in targets.items():
                missing = [m for m in months if m not in have.get(key, set())]
                for month in missing[:budget]:
                    got, go = await self._guarded(f"{query} {month:%Y-%m}",
                                                  self._month_uploads(market, key, query, month))
                    signals.extend(got)
                    budget -= 1
                    if not go or budget <= 0:
                        break
                if not go or budget <= 0:
                    break
        return signals

    @staticmethod
    def _parse_videos(payload: Any) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for it in (payload or {}).get("items", []):
            vid = it.get("id")
            if not vid:
                continue
            stats = it.get("statistics", {})
            snip = it.get("snippet", {})
            details = it.get("contentDetails", {})
            out[vid] = {
                "channel_id": snip.get("channelId"),
                "title": snip.get("title"),
                "views": int(stats["viewCount"]) if "viewCount" in stats else None,
                "published_at": _parse_iso(snip.get("publishedAt")),
                "duration": iso8601_duration_to_seconds(details.get("duration")),
            }
        return out

    @staticmethod
    def _parse_channels(payload: Any) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for it in (payload or {}).get("items", []):
            cid = it.get("id")
            if not cid:
                continue
            stats = it.get("statistics", {})
            snip = it.get("snippet", {})
            hidden = stats.get("hiddenSubscriberCount", False)
            out[cid] = {
                "title": snip.get("title"),
                "subs": None if hidden else int(stats.get("subscriberCount", 0) or 0),
                "total_views": int(stats.get("viewCount", 0) or 0),
                "country": snip.get("country"),
                "created_at": _parse_iso(snip.get("publishedAt")),
            }
        return out
