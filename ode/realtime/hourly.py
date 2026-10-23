"""Same-day breakouts from Wikipedia's public HOURLY pageview dumps.

Daily pageviews tell you about yesterday; the hourly dumps
(dumps.wikimedia.org/other/pageviews) tell you about the last hour. For each
configured language we keep articles above a views threshold for a few days,
then flag those whose last few hours are many times their own normal, or that
appear from nowhere with big numbers. Those are pushed through the normal
discovery filters (no people/films/sports unless enabled), tracked from now
on, and alerted — the first 48 hours of a breakout are worth the most.

Streaming: a dump is ~50 MB gzipped / hundreds of MB raw; it is decompressed
and parsed line by line, never held in memory.
"""

from __future__ import annotations

import statistics
import zlib
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from ode.config import Config
from ode.db import Database
from ode.logging import get_logger
from ode.models import Market

log = get_logger(__name__)

DUMP_URL = ("https://dumps.wikimedia.org/other/pageviews/{y}/{y}-{m}/"
            "pageviews-{y}{m}{d}-{h}0000.gz")


def dump_url(hour: datetime) -> str:
    return DUMP_URL.format(y=f"{hour:%Y}", m=f"{hour:%m}", d=f"{hour:%d}", h=f"{hour:%H}")


def domain_codes(markets: Iterable[Market]) -> dict[str, str]:
    """Dump domain code -> wiki project. 'en' (desktop) and 'en.m' (mobile)
    both belong to en.wikipedia; other projects (en.b, en.d …) are ignored."""
    out: dict[str, str] = {}
    for m in markets:
        lang = m.wiki_project.split(".")[0]
        out[lang] = m.wiki_project
        out[f"{lang}.m"] = m.wiki_project
    return out


def parse_lines(lines: Iterable[bytes], codes: dict[str, str], min_row: int
                ) -> dict[tuple[str, str], int]:
    """Pure: dump lines -> {(wiki, title): views}, desktop + mobile summed.

    Line format: `domain_code page_title count_views total_response_size`.
    Rows below `min_row` are dropped early to bound memory.
    """
    agg: dict[tuple[str, str], int] = defaultdict(int)
    for raw in lines:
        parts = raw.split(b" ")
        if len(parts) < 3:
            continue
        code = parts[0].decode("ascii", "ignore")
        wiki = codes.get(code)
        if wiki is None:
            continue
        try:
            views = int(parts[2])
        except ValueError:
            continue
        if views < min_row:
            continue
        title = parts[1].decode("utf-8", "replace").replace("_", " ")
        if title:  # namespaces / main page are dropped later by the discovery filters
            agg[(wiki, title)] += views
    return agg


async def stream_gzip_lines(client: httpx.AsyncClient, url: str) -> AsyncIterator[bytes] | None:
    """Yield decompressed lines of a remote .gz without buffering the file."""
    async with client.stream("GET", url, timeout=120.0) as resp:
        if resp.status_code == 404:
            return
        resp.raise_for_status()
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
        tail = b""
        async for block in resp.aiter_bytes(1 << 16):
            data = tail + dec.decompress(block)
            *lines, tail = data.split(b"\n")
            for line in lines:
                yield line
        data = tail + dec.flush()
        for line in data.split(b"\n"):
            if line:
                yield line


@dataclass
class Spike:
    wiki: str
    title: str
    window_views: int
    baseline: float | None       # median views of comparable earlier windows
    ratio: float | None          # window / baseline (None = brand new)

    @property
    def label(self) -> str:
        if self.ratio is None:
            return f"NEW: {self.window_views:,} views in the last hours (not seen before)"
        return f"{self.ratio:.0f}x its normal ({self.window_views:,} vs ~{self.baseline:,.0f})"


def detect_spikes(series: dict[tuple[str, str], dict[datetime, int]], now_hour: datetime, *,
                  window_hours: int = 3, baseline_hours: int = 48, spike_ratio: float = 5.0,
                  min_window_views: int = 600, new_article_views: int = 3000) -> list[Spike]:
    """Pure: hourly series -> spikes.

    window   = sum of the last `window_hours` hours ending at `now_hour`.
    baseline = median of the non-overlapping windows of the same length in
               the `baseline_hours` before it (missing hours count as 0 only
               when the article was otherwise seen, i.e. it dropped below the
               storage threshold).
    """
    spikes: list[Spike] = []
    win_start = now_hour - timedelta(hours=window_hours - 1)
    for (wiki, title), hours in series.items():
        window = sum(v for h, v in hours.items() if win_start <= h <= now_hour)
        if window < min_window_views:
            continue
        blocks = []
        for b in range(1, baseline_hours // window_hours + 1):
            end = win_start - timedelta(hours=1 + (b - 1) * window_hours)
            start = end - timedelta(hours=window_hours - 1)
            blocks.append(sum(v for h, v in hours.items() if start <= h <= end))
        seen = [x for x in blocks if x > 0]
        if not seen:
            if window >= new_article_views:
                spikes.append(Spike(wiki, title, window, None, None))
            continue
        base = statistics.median(blocks)
        ratio = window / max(base, 1.0)
        if ratio >= spike_ratio:
            spikes.append(Spike(wiki, title, window, base, round(ratio, 1)))
    spikes.sort(key=lambda s: (s.ratio is not None, -(s.ratio or 0), -s.window_views))
    return spikes


class HourlyIngest:
    def __init__(self, config: Config, db: Database, client: httpx.AsyncClient) -> None:
        self.config = config
        self.db = db
        self.client = client
        self.cfg = config.realtime.get("hourly") or {}

    async def ingest_hour(self, hour: datetime) -> int | None:
        """Ingest one hourly file. None = not published yet; -1 = already
        ingested earlier; 0+ = rows stored now."""
        if await self.db.pool.fetchval("SELECT 1 FROM hourly_ingested WHERE hour = $1", hour):
            return -1
        codes = domain_codes(self.config.markets())
        threshold = int(self.cfg.get("min_views_per_hour", 80))
        url = dump_url(hour)
        lines = []  # parse incrementally in chunks to bound memory
        agg: dict[tuple[str, str], int] = defaultdict(int)
        found = False
        async for line in stream_gzip_lines(self.client, url):
            found = True
            lines.append(line)
            if len(lines) >= 200_000:
                for k, v in parse_lines(lines, codes, max(1, threshold // 4)).items():
                    agg[k] += v
                lines.clear()
        if not found:
            return None
        for k, v in parse_lines(lines, codes, max(1, threshold // 4)).items():
            agg[k] += v
        rows = [(w, t, hour, v) for (w, t), v in agg.items() if v >= threshold]
        async with self.db.pool.acquire() as con, con.transaction():
            await con.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _hv (wiki TEXT, title TEXT, hour TIMESTAMPTZ, "
                "views INTEGER) ON COMMIT DELETE ROWS")
            await con.copy_records_to_table("_hv", records=rows,
                                            columns=["wiki", "title", "hour", "views"])
            await con.execute(
                "INSERT INTO hourly_views SELECT * FROM _hv "
                "ON CONFLICT (wiki, title, hour) DO UPDATE SET views = EXCLUDED.views")
            await con.execute("INSERT INTO hourly_ingested (hour, rows) VALUES ($1, $2) "
                              "ON CONFLICT (hour) DO NOTHING", hour, len(rows))
        log.info("hourly.ingested", hour=str(hour), rows=len(rows))
        return len(rows)

    async def catch_up(self, now: datetime | None = None, back_hours: int = 6) -> list[datetime]:
        """Ingest every published-but-missing hour of the last `back_hours`.
        Returns the hours ingested by THIS call."""
        now = (now or datetime.now(UTC)).replace(minute=0, second=0, microsecond=0)
        done = []
        for i in range(back_hours, 0, -1):
            hour = now - timedelta(hours=i)
            n = await self.ingest_hour(hour)
            if n is not None and n >= 0:
                done.append(hour)
        return done

    async def spikes(self, now_hour: datetime) -> list[Spike]:
        c = self.cfg
        window, baseline = int(c.get("window_hours", 3)), int(c.get("baseline_hours", 48))
        since = now_hour - timedelta(hours=window + baseline)
        rows = await self.db.pool.fetch(
            """
            WITH hot AS (
                SELECT wiki, title FROM hourly_views
                WHERE hour > $1::timestamptz - make_interval(hours => $3::int)
                  AND hour <= $1::timestamptz
                GROUP BY 1, 2 HAVING sum(views) >= $4)
            SELECT h.wiki, h.title, h.hour, h.views FROM hourly_views h
            JOIN hot USING (wiki, title)
            WHERE h.hour > $2::timestamptz AND h.hour <= $1::timestamptz
            """,
            now_hour, since, window, int(c.get("min_window_views", 600)),
        )
        series: dict[tuple[str, str], dict[datetime, int]] = defaultdict(dict)
        for r in rows:
            series[(r["wiki"], r["title"])][r["hour"]] = r["views"]
        return detect_spikes(series, now_hour, window_hours=window, baseline_hours=baseline,
                             spike_ratio=float(c.get("spike_ratio", 5.0)),
                             min_window_views=int(c.get("min_window_views", 600)),
                             new_article_views=int(c.get("new_article_views", 3000)))
