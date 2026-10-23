"""Config loading: YAML (tunables) + environment (secrets).

Secrets NEVER live in YAML. Tunables NEVER live in code. The YAML is
validated on load so a typo fails fast with a clear message instead of
silently producing bad data at 4am.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

from ode.models import Market, Seed
from ode.ratelimit import RateLimit

_REPO_CONFIG = Path(__file__).resolve().parent.parent / "config" / "config.yaml"


class ConfigError(ValueError):
    """Raised when config.yaml is invalid."""


def default_config_path() -> Path:
    """ODE_CONFIG env > ./config/config.yaml > repo checkout's config."""
    env = os.environ.get("ODE_CONFIG")
    if env:
        return Path(env)
    cwd = Path.cwd() / "config" / "config.yaml"
    return cwd if cwd.exists() else _REPO_CONFIG


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
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    n8n_webhook_url: str | None = None
    n8n_webhook_secret: str | None = None
    dashboard_password: str | None = None


class Config:
    """Parsed + validated YAML config with convenience accessors."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._d = data or {}
        self._wiki_overrides: dict[str, str] = self._d.get("wiki_project_overrides", {}) or {}
        self._seeds = self._parse_seeds(self._d.get("seeds", []))
        self._validate()

    # ---------- validation ----------
    @staticmethod
    def _parse_seeds(raw: Any) -> list[Seed]:
        if raw in (None, []):
            return []
        if not isinstance(raw, list):
            raise ConfigError("`seeds` must be a list of {key, name, wiki?, queries?}")
        seeds: list[Seed] = []
        seen: set[str] = set()
        for i, s in enumerate(raw):
            if not isinstance(s, dict) or not s.get("key"):
                raise ConfigError(f"seeds[{i}] needs a `key`")
            key = str(s["key"])
            if not re.fullmatch(r"[a-z0-9_]+", key):
                raise ConfigError(f"seeds[{i}].key {key!r} must be lowercase [a-z0-9_]")
            if key in seen:
                raise ConfigError(f"duplicate seed key {key!r}")
            seen.add(key)
            queries = s.get("queries") or {}
            if not isinstance(queries, dict):
                raise ConfigError(f"seeds[{i}].queries must map language -> query")
            seeds.append(
                Seed(
                    key=key,
                    name=str(s.get("name") or key),
                    wiki=s.get("wiki"),
                    queries={str(k).lower(): str(v) for k, v in queries.items() if v},
                    hn=s.get("hn"),
                    audience=(str(s["audience"]).lower() if s.get("audience") else None),
                )
            )
        return seeds

    def _validate(self) -> None:
        if not self.market_codes:
            raise ConfigError("`markets` must list at least one market")
        for code in self.market_codes:
            try:
                Market.parse(code, self._wiki_overrides)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
        for src, rl in (self._d.get("rate_limits") or {}).items():
            if float(rl.get("rate", 0)) <= 0:
                raise ConfigError(f"rate_limits.{src}.rate must be > 0")
        for pat in self.discovery.get("exclude_title_patterns", []):
            try:
                re.compile(pat)
            except re.error as exc:
                raise ConfigError(f"bad exclude_title_patterns regex {pat!r}: {exc}") from exc

    # ---------- markets ----------
    @property
    def market_codes(self) -> list[str]:
        return list(self._d.get("markets", []) or [])

    def markets(self) -> list[Market]:
        return [Market.parse(c, self._wiki_overrides) for c in self.market_codes]

    def market(self, code: str) -> Market:
        return Market.parse(code, self._wiki_overrides)

    # ---------- seeds ----------
    @property
    def seeds(self) -> list[Seed]:
        return list(self._seeds)

    def queries_for(self, market: Market) -> list[tuple[Seed, str]]:
        """(seed, query) pairs in this market's language. Seeds without a query
        in that language are skipped — an English query would pollute a German
        market's signal."""
        return [(s, s.queries[market.language]) for s in self._seeds if market.language in s.queries]

    # ---------- per-source knobs ----------
    def lookback_days(self, source: str) -> int:
        return int((self._d.get("lookback_days") or {}).get(source, 90))

    def overlap_days(self, source: str) -> int:
        """Days re-fetched on incremental runs (late/revised upstream data)."""
        return int((self._d.get("incremental_overlap_days") or {}).get(source, 2))

    def rate_limit(self, source: str) -> RateLimit:
        rl = (self._d.get("rate_limits") or {}).get(source, {"rate": 1.0, "burst": 1})
        return RateLimit(rate=float(rl["rate"]), burst=int(rl.get("burst", 1)))

    def concurrency(self, source: str) -> int:
        return max(1, int((self._d.get("concurrency") or {}).get(source, 1)))

    @property
    def quotas(self) -> dict[str, int | None]:
        return dict(self._d.get("quotas") or {})

    @property
    def youtube_unit_cost(self) -> dict[str, int]:
        return dict(
            self._d.get("youtube_unit_cost") or {"search": 100, "videos": 1, "channels": 1}
        )

    @property
    def youtube(self) -> dict[str, Any]:
        return dict(self._d.get("youtube") or {})

    @property
    def reddit(self) -> dict[str, Any]:
        return dict(self._d.get("reddit") or {})

    def reddit_scope(self, market: Market) -> dict[str, Any] | None:
        return (self.reddit.get("scopes") or {}).get(market.code)

    @property
    def discovery(self) -> dict[str, Any]:
        return dict(self._d.get("discovery") or {})

    # ---------- http ----------
    @property
    def _http(self) -> dict[str, Any]:
        return self._d.get("http") or {}

    @property
    def http_timeout(self) -> float:
        return float(self._http.get("timeout_seconds", 30))

    @property
    def http_max_retries(self) -> int:
        return int(self._http.get("max_retries", 4))

    @property
    def http_backoff_base(self) -> float:
        return float(self._http.get("backoff_base_seconds", 2.0))

    @property
    def circuit_breaker_failures(self) -> int:
        return int(self._http.get("circuit_breaker_failures", 10))

    @property
    def scoring(self) -> dict[str, Any]:
        return dict(self._d.get("scoring") or {})

    @property
    def monetization(self) -> dict[str, Any]:
        return dict(self._d.get("monetization") or {})

    def market_value(self, code: str) -> float:
        """Relative value of a visitor in this market (US = 1.0). Default 0.5."""
        return float((self._d.get("market_value") or {}).get(code, 0.5))

    @property
    def playstore(self) -> dict[str, Any]:
        return dict(self._d.get("playstore") or {})

    @property
    def gemini(self) -> dict[str, Any]:
        return dict(self._d.get("gemini") or {})

    @property
    def realtime(self) -> dict[str, Any]:
        return dict(self._d.get("realtime") or {})

    @property
    def alerts(self) -> dict[str, Any]:
        return dict(self._d.get("alerts") or {})

    @property
    def seasonal(self) -> dict[str, Any]:
        return dict(self._d.get("seasonal") or {})

    @property
    def scheduler(self) -> dict[str, Any]:
        return dict(self._d.get("scheduler") or {})

    @property
    def actions(self) -> dict[str, Any]:
        return dict(self._d.get("actions") or {})

    @property
    def content(self) -> dict[str, Any]:
        return dict(self._d.get("content") or {})

    @property
    def dollars(self) -> dict[str, Any]:
        return dict(self._d.get("dollars") or {})

    @property
    def dataforseo(self) -> dict[str, Any]:
        return dict(self._d.get("dataforseo") or {})

    @property
    def learning(self) -> dict[str, Any]:
        return dict(self._d.get("learning") or {})

    @property
    def export(self) -> dict[str, Any]:
        return dict(self._d.get("export") or {})

    @property
    def backtest(self) -> dict[str, Any]:
        return dict(self._d.get("backtest") or {})

    @property
    def miners(self) -> dict[str, Any]:
        return dict(self._d.get("miners") or {})

    @property
    def dashboard(self) -> dict[str, Any]:
        return dict(self._d.get("dashboard") or {})

    def seed_by_key(self, topic_key: str) -> Seed | None:
        return next((s for s in self._seeds if s.topic_key == topic_key), None)

    def raw(self) -> dict[str, Any]:
        return self._d


def load_config(path: Path | str | None = None) -> Config:
    p = Path(path) if path else default_config_path()
    try:
        with open(p, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found at {p} (set ODE_CONFIG)") from exc
    return Config(data)


@lru_cache(maxsize=1)
def get_secrets() -> Secrets:
    return Secrets()
