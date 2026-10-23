"""Google Trends "trending now" per country (public RSS, no key).

What people are searching for RIGHT NOW in each market's country, with an
approximate search count. High-traffic items are matched to a Wikipedia
article in that market's language, filtered like every other discovered
topic, tracked from then on, and alerted (perfect for fast AI Shorts).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from ode.http import HttpFetcher
from ode.models import Market

DEFAULT_URL = "https://trends.google.com/trending/rss?geo={geo}"


@dataclass(frozen=True)
class TrendItem:
    title: str
    traffic: int | None
    news: tuple[str, ...] = ()


def parse_traffic(text: str | None) -> int | None:
    """'200K+' / '2,000+' / '1M+' -> int."""
    if not text:
        return None
    m = re.match(r"\s*([\d.,]+)\s*([KkMm]?)", text)
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    mult = {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1)
    return int(num * mult)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_rss(xml_text: str) -> list[TrendItem]:
    """Pure: RSS XML -> trend items (namespace-agnostic, tolerant)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items: list[TrendItem] = []
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        title, traffic, news = None, None, []
        for child in item:
            name = _local(child.tag)
            if name == "title" and child.text:
                title = child.text.strip()
            elif name == "approx_traffic":
                traffic = parse_traffic(child.text)
            elif name == "news_item":
                for sub in child:
                    if _local(sub.tag) == "news_item_title" and sub.text:
                        news.append(sub.text.strip())
        if title:
            items.append(TrendItem(title, traffic, tuple(news[:3])))
    return items


async def fetch_trends(fetcher: HttpFetcher, market: Market, url_template: str = DEFAULT_URL
                       ) -> list[TrendItem]:
    url = url_template.format(geo=market.region)
    await fetcher.bucket.acquire()
    resp = await fetcher.client.get(url, timeout=30.0)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return parse_rss(resp.text)


async def wiki_match(fetcher: HttpFetcher, market: Market, query: str) -> str | None:
    """Best Wikipedia article for a trending search, in the market's language."""
    payload: Any = await fetcher.get_json(
        f"https://{market.wiki_host}/w/api.php",
        params={"action": "query", "list": "search", "srsearch": query, "srlimit": "1",
                "srnamespace": "0", "srprop": "", "format": "json", "formatversion": "2"})
    hits = ((payload or {}).get("query") or {}).get("search") or []
    return hits[0]["title"] if hits and hits[0].get("title") else None
