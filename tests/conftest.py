from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

from ode.config import Config, Secrets

FAST_HTTP = {"timeout_seconds": 5, "max_retries": 3, "backoff_base_seconds": 0.001,
             "circuit_breaker_failures": 5}


def make_config(**overrides: Any) -> Config:
    fast = {"rate": 10_000.0, "burst": 10_000}
    data: dict[str, Any] = {
        "markets": ["en-US", "de-DE"],
        "wiki_project_overrides": {"en-US": "en.wikipedia"},
        "seeds": [
            {"key": "raspberry_pi", "name": "Raspberry Pi", "wiki": "Raspberry Pi",
             "queries": {"en": "raspberry pi project", "de": "raspberry pi projekt"},
             "hn": "Raspberry Pi"},
            {"key": "n8n", "name": "n8n", "queries": {"en": "n8n automation"}, "hn": "n8n"},
        ],
        "discovery": {
            "top_days_back": 1, "bootstrap_days_back": 2, "morelike_per_seed": 5,
            "max_tracked_per_market": 100, "stale_after_days": 30,
            "exclude_wikidata_classes": {"Q5": "human", "Q476028": "football club"},
            "exclude_title_patterns": [r"^\d{4}[ _](?!\()"],
            "exclude_titles": ["Main Page", "-"],
        },
        "reddit": {"max_pages": 3, "unanswered_min_age_hours": 48,
                   "scopes": {"en-US": {"mode": "global"},
                              "de-DE": {"subreddits": ["de", "de_EDV"]}}},
        "youtube": {"max_results": 5},
        "lookback_days": {"wikipedia": 30, "youtube": 30, "reddit": 30, "hackernews": 5},
        "incremental_overlap_days": {"wikipedia": 3, "hackernews": 2, "reddit": 2},
        "rate_limits": {s: fast for s in
                        ["wikipedia", "wikidata", "youtube", "reddit", "hackernews"]},
        "concurrency": {"wikipedia": 4, "hackernews": 4},
        "quotas": {"youtube": 9000, "reddit": None, "wikipedia": None, "hackernews": None},
        "youtube_unit_cost": {"search": 100, "videos": 1, "channels": 1},
        "http": FAST_HTTP,
    }
    data.update(overrides)
    return Config(data)


@pytest.fixture
def config() -> Config:
    return make_config()


@pytest.fixture
def secrets() -> Secrets:
    return Secrets(_env_file=None, database_url="postgresql://unused")  # type: ignore[call-arg]


# ---------------------------------------------------------------- real DB
TEST_DSN = os.environ.get("ODE_TEST_DATABASE_URL")


@pytest.fixture
async def db() -> AsyncIterator[Any]:
    """A fresh, empty `ode_test` database with the full schema applied."""
    if not TEST_DSN:
        pytest.skip("set ODE_TEST_DATABASE_URL to run database integration tests")
    import asyncpg

    from ode.db import Database

    admin = await asyncpg.connect(TEST_DSN)
    try:
        await admin.execute("DROP DATABASE IF EXISTS ode_test WITH (FORCE)")
        await admin.execute("CREATE DATABASE ode_test")
    finally:
        await admin.close()
    base, _, _ = TEST_DSN.rpartition("/")
    database = await Database.connect(f"{base}/ode_test")
    await database.init_schema()
    try:
        yield database
    finally:
        await database.close()
