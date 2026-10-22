"""Wikipedia / Wikidata metadata: top articles, title -> Wikidata id, entity
classes and sitelinks (the same topic's title in every language), and
"morelike" neighbours.

Parsers are pure functions over the documented API payloads so they are
testable offline; `WikiMeta` does the (batched, rate-limited) fetching.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ode.http import HttpFetcher

TOP_URL = "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/{project}/all-access/{y}/{m}/{d}"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
BATCH = 50  # MediaWiki limit for titles= / ids= per request

# Sitelinks that are not language Wikipedias.
_NON_LANGUAGE_SITES = {
    "commonswiki", "specieswiki", "metawiki", "mediawikiwiki", "wikidatawiki",
    "sourceswiki", "incubatorwiki", "outreachwiki", "wikimaniawiki",
    "wikifunctionswiki", "foundationwiki", "testwiki", "test2wiki",
}


def norm_title(title: str) -> str:
    """Canonical internal form: spaces, not underscores; trimmed."""
    return title.replace("_", " ").strip()


@dataclass(frozen=True)
class PageInfo:
    title: str             # canonical title after normalization + redirects
    ns: int
    qid: str | None
    missing: bool = False


@dataclass(frozen=True)
class EntityInfo:
    qid: str
    label_en: str | None
    p31: tuple[str, ...]
    sitelinks: dict[str, str] = field(default_factory=dict)  # 'dewiki' -> title

    @property
    def wiki_langs(self) -> list[str]:
        langs = [
            s[:-4].replace("_", "-")
            for s in self.sitelinks
            if s.endswith("wiki") and s not in _NON_LANGUAGE_SITES
        ]
        return sorted(langs)


# ---------------------------------------------------------------- parsers
def parse_top(payload: Any) -> list[tuple[str, int]]:
    """Top-articles payload -> [(title, views)] in rank order."""
    if not isinstance(payload, dict):
        return []
    out: list[tuple[str, int]] = []
    for item in payload.get("items") or []:
        for art in item.get("articles") or []:
            title, views = art.get("article"), art.get("views")
            if not title or views is None:
                continue
            try:
                out.append((norm_title(str(title)), int(views)))
            except (TypeError, ValueError):
                continue
    return out


def parse_pageprops(payload: Any, inputs: Iterable[str]) -> dict[str, PageInfo]:
    """action=query&prop=pageprops (formatversion=2) -> {input title: PageInfo}.

    Follows the `normalized` and `redirects` maps so every input resolves to
    its canonical page. Inputs with no page in the response are omitted.
    """
    if not isinstance(payload, dict):
        return {}
    q = payload.get("query") or {}
    hop: dict[str, str] = {}
    for entry in (q.get("normalized") or []) + (q.get("redirects") or []):
        if entry.get("from") and entry.get("to"):
            hop[entry["from"]] = entry["to"]
    pages: dict[str, PageInfo] = {}
    for page in q.get("pages") or []:
        title = page.get("title")
        if not title:
            continue
        pages[title] = PageInfo(
            title=title,
            ns=int(page.get("ns", 0)),
            qid=(page.get("pageprops") or {}).get("wikibase_item"),
            missing=bool(page.get("missing") or page.get("invalid")),
        )
    out: dict[str, PageInfo] = {}
    for raw in inputs:
        # The API's `normalized.from` is exactly what we sent; fall back to our
        # own normalization only if the raw form isn't mentioned at all.
        t = raw if (raw in hop or raw in pages) else norm_title(raw)
        seen: set[str] = set()
        while t in hop and t not in seen:  # follow normalize -> redirect chain
            seen.add(t)
            t = hop[t]
        if t in pages:
            out[raw] = pages[t]
    return out


def parse_entities(payload: Any) -> dict[str, EntityInfo]:
    """wbgetentities (props=claims|sitelinks|labels) -> {qid: EntityInfo}."""
    if not isinstance(payload, dict):
        return {}
    out: dict[str, EntityInfo] = {}
    for qid, ent in (payload.get("entities") or {}).items():
        if not isinstance(ent, dict) or "missing" in ent:
            continue
        p31: list[str] = []
        for claim in (ent.get("claims") or {}).get("P31") or []:
            value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
            if isinstance(value, dict) and value.get("id"):
                p31.append(value["id"])
        sitelinks = {
            site: link["title"]
            for site, link in (ent.get("sitelinks") or {}).items()
            if isinstance(link, dict) and link.get("title")
        }
        label = ((ent.get("labels") or {}).get("en") or {}).get("value")
        out[qid] = EntityInfo(qid=qid, label_en=label, p31=tuple(p31), sitelinks=sitelinks)
    return out


def parse_morelike(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    hits = (payload.get("query") or {}).get("search") or []
    return [norm_title(h["title"]) for h in hits if h.get("title") and int(h.get("ns", 0)) == 0]


# ---------------------------------------------------------------- filtering
class TitleFilter:
    """Cheap pre-API filter: exact titles + regex patterns from config."""

    def __init__(self, exclude_titles: Iterable[str], patterns: Iterable[str]) -> None:
        self.titles = {norm_title(t).casefold() for t in exclude_titles}
        self.patterns = [re.compile(p) for p in patterns]

    def reason(self, title: str) -> str | None:
        t = norm_title(title)
        if not t or t.casefold() in self.titles:
            return "excluded title"
        for p in self.patterns:
            if p.search(t) or p.search(t.replace(" ", "_")):
                return f"title pattern {p.pattern}"
        return None


def exclusion_reason(entity: EntityInfo | None, excluded_classes: dict[str, str]) -> str | None:
    """Reason a Wikidata entity is not a content opportunity, or None."""
    if entity is None:
        return None
    for cls in entity.p31:
        if cls in excluded_classes:
            return f"class {cls} ({excluded_classes[cls]})"
    return None


# ---------------------------------------------------------------- fetching
def _chunks(items: list[str], n: int = BATCH) -> Iterable[list[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


class WikiMeta:
    """Batched fetchers. `wiki` = Wikipedia/Wikimedia host family, `wd` = Wikidata."""

    def __init__(self, wiki: HttpFetcher, wd: HttpFetcher) -> None:
        self.wiki = wiki
        self.wd = wd

    async def top_articles(self, project: str, day: date) -> list[tuple[str, int]] | None:
        """None when the day isn't published yet (404)."""
        url = TOP_URL.format(project=project, y=f"{day:%Y}", m=f"{day:%m}", d=f"{day:%d}")
        payload = await self.wiki.get_json(url)
        return None if payload is None else parse_top(payload)

    async def resolve_titles(self, host: str, titles: list[str]) -> dict[str, PageInfo]:
        out: dict[str, PageInfo] = {}
        for chunk in _chunks(sorted(set(titles))):
            payload = await self.wiki.get_json(
                f"https://{host}/w/api.php",
                params={
                    "action": "query", "prop": "pageprops", "ppprop": "wikibase_item",
                    "redirects": "1", "titles": "|".join(chunk), "format": "json",
                    "formatversion": "2", "maxlag": "5",
                },
            )
            out.update(parse_pageprops(payload, chunk))
        return out

    async def entities(self, qids: list[str]) -> dict[str, EntityInfo]:
        out: dict[str, EntityInfo] = {}
        for chunk in _chunks(sorted(set(qids))):
            payload = await self.wd.get_json(
                WIKIDATA_API,
                params={
                    "action": "wbgetentities", "ids": "|".join(chunk),
                    "props": "claims|sitelinks|labels", "languages": "en",
                    "format": "json", "maxlag": "5",
                },
            )
            out.update(parse_entities(payload))
        return out

    async def morelike(self, host: str, title: str, limit: int) -> list[str]:
        payload = await self.wiki.get_json(
            f"https://{host}/w/api.php",
            params={
                "action": "query", "list": "search", "srsearch": f"morelike:{title}",
                "srnamespace": "0", "srlimit": str(limit), "srprop": "",
                "format": "json", "formatversion": "2", "maxlag": "5",
            },
        )
        return parse_morelike(payload)
