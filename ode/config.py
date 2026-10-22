"""Config loading: YAML (tunables) + environment (secrets).

Secrets NEVER live in YAML. Tunables NEVER live in code.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

from ode.models import Market
from ode.ratelimit import RateLimit

DEFAULT_CONFIG_PATH = Path(
    os.environ.get("ODE_CONFIG", Path(__file__).resolve().parent.parent / "config" / "config.yaml")
)


class Secrets(BaseSettings):
    """Loaded from environment / .env. Missing keys are allowed (collectors skip)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    database_url: str = "postgresql://ode:ode@localhost:5432/ode"
    youtube_api_key: str | None = None
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str = "ode:discovery-engine:v0.1"
    dataforseo_login: str | None = None
    dataforseo_password: str | None = None
    gemini_api_key: str | None = None


class Config:
    """Parsed YAML config with convenience accessors."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._d = data
        self._wiki_overrides: dict[str, str] = data.get("wiki_project_overrides", {})

    # -- markets --
    @property
    def market_codes(self) -> list[str]:
        return list(self._d.get("markets", []))

    def markets(self) -> list[Market]:
        return [Market.parse(c, self._wiki_overrides) for c in self.market_codes]

    def market(self, code: str) -> Market:
        return Market.parse(code, self._wiki_overrides)

    # -- seeds --
    @property
    def seed_queries(self) -> list[str]:
        return list(self._d.get("seeds", {}).get("queries", []))

    @property
    def seed_wiki_articles(self) -> list[str]:
        return list(self._d.get("seeds", {}).get("wiki_articles", []))

    # -- per-source knobs --
    def lookback_days(self, source: str) -> int:
        return int(self._d.get("lookback_days", {}).get(source, 90))

    def rate_limit(self, source: str) -> RateLimit:
        rl = self._d.get("rate_limits", {}).get(source, {"rate": 1.0, "burst": 1})
        return RateLimit(rate=float(rl["rate"]), burst=int(rl.get("burst", 1)))

    @property
    def quotas(self) -> dict[str, int | None]:
        return dict(self._d.get("quotas", {}))

    @property
    def youtube_unit_cost(self) -> dict[str, int]:
        return dict(self._d.get("youtube_unit_cost", {"search": 100, "videos": 1, "channels": 1}))

    # -- http --
    @property
    def http_timeout(self) -> float:
        return float(self._d.get("http", {}).get("timeout_seconds", 30))

    @property
    def http_max_retries(self) -> int:
        return int(self._d.get("http", {}).get("max_retries", 4))

    @property
    def http_backoff_base(self) -> float:
        return float(self._d.get("http", {}).get("backoff_base_seconds", 2.0))

    @property
    def scoring(self) -> dict[str, Any]:
        return dict(self._d.get("scoring", {}))

    def raw(self) -> dict[str, Any]:
        return self._d


def load_config(path: Path | str | None = None) -> Config:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(p, "r", encoding="utf-8") as fh:
        return Config(yaml.safe_load(fh) or {})


@lru_cache(maxsize=1)
def get_secrets() -> Secrets:
    return Secrets()
