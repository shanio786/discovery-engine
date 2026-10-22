"""YouTube Data API v3 collector.

Harvests outlier videos (views >> what the channel's sub count predicts) and
channel stats per seed query/market. Enforces the persisted daily quota: it
reserves units BEFORE every call and stops cleanly if the cap would break,
rather than failing mid-run. Self-skips when no API key is set.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

from ode.collectors.base import BaseCollector
from ode.http import CircuitOpenError
from ode.models import Market, RawSignal, Seed

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

    def supports(self, market: Market) -> tuple[bool, str]:
        if not self.config.queries_for(market):
            return False, f"no seed queries in language '{market.language}'"
        return True, ""

    async def _ordered_queries(self, market: Market) -> list[tuple[Seed, str]]:
        """Least-recently-searched first, so a quota-starved day still rotates
        through every seed over a few days instead of always the same ones."""
        last = await self.state.last_dates(market.code, self.name, "youtube_video_count")
        pairs = self.config.queries_for(market)
        return sorted(pairs, key=lambda p: (last.get(p[0].topic_key) or date.min, p[0].key))

    async def collect(self, market: Market, since: datetime) -> list[RawSignal]:
        key = self.secrets.youtube_api_key
        cost = self.config.youtube_unit_cost
        max_results = int(self.config.youtube.get("max_results", 25))
        observed = datetime.now(UTC).date()
        published_after = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        signals: list[RawSignal] = []

        for seed, query in await self._ordered_queries(market):
            if self.aborted:
                break
            if not await self.charge_quota(cost["search"]):
                self.notes.append(f"daily quota reached before '{query}'")
                self.log.warning("youtube.quota_reached", query=query)
                break
            try:
                search = await self.http.get_json(
                    SEARCH,
                    params={
                        "key": key, "q": query, "part": "id", "type": "video",
                        "order": "viewCount", "maxResults": max_results,
                        "regionCode": market.region, "relevanceLanguage": market.language,
                        "publishedAfter": published_after,
                    },
                )
                video_ids = [
                    it["id"]["videoId"]
                    for it in (search or {}).get("items", [])
                    if (it.get("id") or {}).get("videoId")
                ]
                videos: dict[str, dict[str, Any]] = {}
                channels: dict[str, dict[str, Any]] = {}
                if video_ids:
                    if not await self.charge_quota(cost["videos"]):
                        self.notes.append("daily quota reached at videos.list")
                        break
                    videos = self._parse_videos(await self.http.get_json(
                        VIDEOS,
                        params={"key": key, "part": "statistics,snippet,contentDetails",
                                "id": ",".join(video_ids)},
                    ))
                    channel_ids = sorted({v["channel_id"] for v in videos.values()
                                          if v.get("channel_id")})
                    if channel_ids:
                        if not await self.charge_quota(cost["channels"]):
                            self.notes.append("daily quota reached at channels.list")
                            break
                        channels = self._parse_channels(await self.http.get_json(
                            CHANNELS,
                            params={"key": key, "part": "statistics,snippet",
                                    "id": ",".join(channel_ids)},
                        ))
            except CircuitOpenError as exc:
                self.aborted = str(exc)
                break
            except Exception as exc:  # noqa: BLE001 - one query must not kill the run
                self.errors += 1
                self.log.warning("youtube.query_failed", query=query, error=str(exc)[:300])
                continue
            signals.extend(build_signals(videos, channels, market=market.code, query=query,
                                         observed=observed, topic_key=seed.topic_key))
            self.log.debug("youtube.query_ok", query=query, videos=len(videos))
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
