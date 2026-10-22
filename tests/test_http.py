import httpx
import pytest
import respx

from ode.http import CircuitOpenError, HttpFetcher, UpstreamError, redact_url
from ode.ratelimit import RateLimit, RetryableHTTPError

URL = "https://api.example.com/v1/thing"


def make(client, **kw):
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    f = HttpFetcher(client, RateLimit(10_000, 10_000), name="t", max_retries=3,
                    backoff_base=0.001, breaker_threshold=kw.pop("breaker", 3),
                    sleep=fake_sleep, **kw)
    return f, sleeps


def test_redact_url_hides_secrets():
    out = redact_url("https://x.com/a?key=SECRET&q=hi", {"access_token": "T", "part": "id"})
    assert "SECRET" not in out and "=T" not in out
    assert "q=hi" in out and "part=id" in out and out.count("%2A%2A%2A") == 2


@respx.mock
async def test_404_returns_none_and_counts_as_success():
    respx.get(URL).mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as c:
        f, _ = make(c)
        f.consecutive_failures = 2
        assert await f.get_json(URL) is None
        assert f.consecutive_failures == 0


@respx.mock
async def test_5xx_is_retried_then_raises():
    route = respx.get(URL).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as c:
        f, _ = make(c)
        with pytest.raises(RetryableHTTPError):
            await f.get_json(URL)
    assert route.call_count == 3


@respx.mock
async def test_429_respects_retry_after_then_succeeds():
    respx.get(URL).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={"ok": 1})])
    async with httpx.AsyncClient() as c:
        f, sleeps = make(c)
        assert await f.get_json(URL) == {"ok": 1}
    assert sleeps == [7.0]


@respx.mock
async def test_4xx_is_not_retried_and_never_leaks_key():
    route = respx.get(URL).mock(return_value=httpx.Response(403))
    async with httpx.AsyncClient() as c:
        f, _ = make(c)
        with pytest.raises(UpstreamError) as ei:
            await f.get_json(URL, params={"key": "SUPERSECRET"})
    assert route.call_count == 1
    assert "SUPERSECRET" not in str(ei.value)


@respx.mock
async def test_proxy_error_not_retried_and_redacted():
    route = respx.get(URL).mock(side_effect=httpx.ProxyError("403 Forbidden"))
    async with httpx.AsyncClient() as c:
        f, _ = make(c)
        with pytest.raises(UpstreamError) as ei:
            await f.get_json(URL, params={"key": "SUPERSECRET"})
    assert route.call_count == 1
    assert "SUPERSECRET" not in str(ei.value)


@respx.mock
async def test_timeouts_are_retried():
    route = respx.get(URL).mock(side_effect=[httpx.ReadTimeout("slow"),
                                             httpx.Response(200, json=[1])])
    async with httpx.AsyncClient() as c:
        f, _ = make(c)
        assert await f.get_json(URL) == [1]
    assert route.call_count == 2


@respx.mock
async def test_maxlag_in_body_is_retried():
    respx.get(URL).mock(side_effect=[httpx.Response(200, json={"error": {"code": "maxlag"}}),
                                     httpx.Response(200, json={"query": {}})])
    async with httpx.AsyncClient() as c:
        f, _ = make(c)
        assert await f.get_json(URL) == {"query": {}}


@respx.mock
async def test_circuit_breaker_opens_after_consecutive_failures():
    respx.get(URL).mock(return_value=httpx.Response(400))
    async with httpx.AsyncClient() as c:
        f, _ = make(c, breaker=3)
        for _ in range(3):
            with pytest.raises(UpstreamError):
                await f.get_json(URL)
        with pytest.raises(CircuitOpenError):
            await f.get_json(URL)


@respx.mock
async def test_non_json_is_upstream_error():
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>"))
    async with httpx.AsyncClient() as c:
        f, _ = make(c)
        with pytest.raises(UpstreamError):
            await f.get_json(URL)
