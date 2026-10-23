"""Shared HTTP machinery for collectors and discovery.

One `HttpFetcher` per upstream host family. It owns:
  * the token bucket (politeness),
  * retries with exponential backoff for transient failures only,
  * Retry-After handling on 429,
  * a circuit breaker so a dead or blocked host aborts the run in seconds
    instead of grinding through thousands of items,
  * secret redaction, so API keys never reach logs or collector_runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ode.ratelimit import AsyncTokenBucket, RateLimit, RetryableHTTPError, with_backoff

SECRET_PARAMS = {"key", "api_key", "apikey", "token", "access_token", "client_secret", "password",
                 "x-goog-api-key"}
MAX_RETRY_AFTER_SECONDS = 60.0


class UpstreamError(Exception):
    """Non-retryable upstream failure. Message is always secret-free."""


class CircuitOpenError(Exception):
    """Too many consecutive failures: the host is down or blocked. Stop the run."""


def redact_url(url: str, params: dict[str, Any] | None = None) -> str:
    """Return `url` (+ params) with secret query values replaced by '***'."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if params:
        query += [(k, str(v)) for k, v in params.items()]
    safe = [(k, "***" if k.lower() in SECRET_PARAMS else v) for k, v in query]
    return urlunsplit(parts._replace(query=urlencode(safe)))


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return min(max(float(value), 0.0), MAX_RETRY_AFTER_SECONDS)
    except ValueError:
        return None  # HTTP-date form — fall back to our own backoff


class HttpFetcher:
    """Rate-limited, retrying, circuit-broken JSON fetcher for one host family."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        limit: RateLimit,
        *,
        name: str,
        max_retries: int = 4,
        backoff_base: float = 2.0,
        breaker_threshold: int = 10,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.bucket = AsyncTokenBucket.from_limit(limit)
        self.name = name
        self.max_retries = max(1, max_retries)
        self.backoff_base = backoff_base
        self.breaker_threshold = max(1, breaker_threshold)
        self._sleep = sleep
        self.consecutive_failures = 0
        self.requests = 0

    @property
    def circuit_open(self) -> bool:
        return self.consecutive_failures >= self.breaker_threshold

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
        json_body: Any | None = None,
        auth: tuple[str, str] | None = None,
        cost_units: int = 1,
    ) -> Any | None:
        """Return parsed JSON, or None on 404. Raises UpstreamError /
        CircuitOpenError / the last transient error after retries."""
        if self.circuit_open:
            raise CircuitOpenError(
                f"{self.name}: {self.consecutive_failures} consecutive failures, giving up"
            )
        safe = redact_url(url, params)

        @with_backoff(max_attempts=self.max_retries, base_seconds=self.backoff_base)
        async def _do() -> Any | None:
            await self.bucket.acquire(cost_units)
            self.requests += 1
            resp = await self.client.request(
                method, url, params=params, headers=headers, data=data, json=json_body,
                auth=auth,
            )
            if resp.status_code == 404:
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = _retry_after_seconds(resp)
                if wait:
                    await self._sleep(wait)
                raise RetryableHTTPError(f"HTTP {resp.status_code} from {safe}")
            if resp.status_code >= 400:
                raise UpstreamError(f"HTTP {resp.status_code} from {safe}")
            try:
                payload = resp.json()
            except ValueError as exc:
                raise UpstreamError(f"non-JSON response from {safe}") from exc
            # MediaWiki/Wikidata signal replica lag in-band with HTTP 200.
            if isinstance(payload, dict):
                err = payload.get("error")
                if isinstance(err, dict) and err.get("code") == "maxlag":
                    raise RetryableHTTPError(f"maxlag from {safe}")
            return payload

        try:
            result = await _do()
        except httpx.HTTPError as exc:
            # Transport errors can embed the full URL; never let that escape.
            self.consecutive_failures += 1
            raise UpstreamError(f"{type(exc).__name__} for {safe}") from None
        except Exception:
            self.consecutive_failures += 1
            raise
        self.consecutive_failures = 0
        return result

    async def get_json(self, url: str, **kwargs: Any) -> Any | None:
        return await self.request_json("GET", url, **kwargs)
