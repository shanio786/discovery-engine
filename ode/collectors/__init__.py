"""Collector registry. New sources register here without touching the pipeline."""

from __future__ import annotations

from ode.collectors.base import BaseCollector
from ode.collectors.hackernews import HackerNewsCollector
from ode.collectors.reddit import RedditCollector
from ode.collectors.wikipedia import WikipediaCollector
from ode.collectors.youtube import YouTubeCollector

# Order = daily run order. Free + keyless first, keyed last.
COLLECTOR_CLASSES: list[type[BaseCollector]] = [
    WikipediaCollector,
    HackerNewsCollector,
    YouTubeCollector,
    RedditCollector,
]

__all__ = [
    "COLLECTOR_CLASSES",
    "BaseCollector",
    "HackerNewsCollector",
    "RedditCollector",
    "WikipediaCollector",
    "YouTubeCollector",
]
