"""Core domain types shared across collectors, discovery and the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class Market:
    """A language+region target. `wiki_project` is the Wikipedia host."""

    code: str          # "en-US"
    language: str      # "en"
    region: str        # "US"
    wiki_project: str  # "en.wikipedia"

    @classmethod
    def parse(cls, code: str, wiki_overrides: dict[str, str] | None = None) -> Market:
        parts = code.split("-")
        if len(parts) != 2 or not parts[0].isalpha() or not parts[1].isalpha():
            raise ValueError(f"market code must be '<lang>-<REGION>', got {code!r}")
        language, region = parts[0].lower(), parts[1].upper()
        wiki = (wiki_overrides or {}).get(code) or f"{language}.wikipedia"
        return cls(code=code, language=language, region=region, wiki_project=wiki)

    @property
    def wiki_host(self) -> str:
        """'en.wikipedia' -> 'en.wikipedia.org' (for the action API)."""
        return f"{self.wiki_project}.org"

    @property
    def wiki_site(self) -> str:
        """Wikidata sitelink id, e.g. 'enwiki', 'dewiki'."""
        return f"{self.wiki_project.split('.')[0]}wiki"


@dataclass(frozen=True)
class Seed:
    """An operator seed topic. `queries` are keyed by language ('en', 'de')."""

    key: str
    name: str
    wiki: str | None = None                       # English Wikipedia title
    queries: dict[str, str] = field(default_factory=dict)
    hn: str | None = None                         # short core term for Hacker News

    @property
    def hn_term(self) -> str:
        return self.hn or self.name

    @property
    def topic_key(self) -> str:
        return f"seed:{self.key}"


@dataclass(frozen=True)
class WatchItem:
    """One tracked term for a market (from the watchlist)."""

    term: str
    topic_key: str
    origin: str = "seed"


@dataclass
class RawSignal:
    """One normalized observation emitted by a collector.

    The pipeline resolves the signal to a topic — by `topic_key` when given
    (stable across languages), else by `market`+`topic_surface_form` — then
    upserts a `topic_metrics` row keyed on `dedup_key()`, which is what makes
    re-runs idempotent. An empty `metric_name` marks an entity-only signal that
    stores a video/channel but writes no time-series row.
    """

    source: str
    market: str
    topic_surface_form: str
    metric_name: str
    value: float
    observed_at: date
    raw: dict[str, Any] = field(default_factory=dict)
    video: dict[str, Any] | None = None
    channel: dict[str, Any] | None = None
    topic_key: str | None = None

    def dedup_key(self) -> tuple[str, str, str, str, date]:
        return (
            self.source,
            self.market,
            self.topic_key or self.topic_surface_form,
            self.metric_name,
            self.observed_at,
        )
