from __future__ import annotations

import pytest

from ode.config import Config, Secrets


@pytest.fixture
def config() -> Config:
    # Minimal in-memory config; avoids depending on the on-disk YAML in tests.
    return Config(
        {
            "markets": ["en-US", "de-DE"],
            "wiki_project_overrides": {"en-US": "en.wikipedia", "de-DE": "de.wikipedia"},
            "seeds": {"queries": ["n8n automation"], "wiki_articles": ["Raspberry_Pi"]},
            "lookback_days": {"wikipedia": 30, "youtube": 30, "reddit": 30, "hackernews": 30},
            "rate_limits": {
                "wikipedia": {"rate": 1000.0, "burst": 1000},
                "youtube": {"rate": 1000.0, "burst": 1000},
                "reddit": {"rate": 1000.0, "burst": 1000},
                "hackernews": {"rate": 1000.0, "burst": 1000},
            },
            "quotas": {"youtube": 9000, "reddit": None, "wikipedia": None, "hackernews": None},
            "youtube_unit_cost": {"search": 100, "videos": 1, "channels": 1},
            "http": {"timeout_seconds": 5, "max_retries": 2, "backoff_base_seconds": 0.01},
        }
    )


@pytest.fixture
def secrets() -> Secrets:
    return Secrets(_env_file=None)  # type: ignore[call-arg]
