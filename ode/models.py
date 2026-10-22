"""Core domain types shared across collectors and the pipeline."""

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
        try:
            language, region = code.split("-")
        except ValueError as exc:  # pragma: no cover - guarded by config validation
            raise ValueError(f"market code must be '<lang>-<REGION>', got {code!r}") from exc
        wiki = (wiki_overrides or {}).get(code) or f"{language}.wikipedia"
        return cls(code=code, language=language, region=region, wiki_project=wiki)


@dataclass
class RawSignal:
    """One normalized observation emitted by a collector.

    The pipeline resolves `topic_surface_form`+`market` to a topic_id, then
    upserts a `topic_metrics` row keyed on `dedup_key()` — which is what makes
    re-runs idempotent. Optional `video`/`channel` payloads carry YouTube
    entities for the videos/channels tables.
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

    def dedup_key(self) -> tuple[str, str, str, str, date]:
        return (
            self.source,
            self.market,
            self.topic_surface_form,
            self.metric_name,
            self.observed_at,
        )
