"""Tests for resources/sso_client.py — fully offline via httpx.MockTransport.

Every test runs with STATUTES_IGNORE_WINDOW=1 (no live window dependency)
and STATUTES_NO_DELAY=1 (no pacing/backoff sleeps). No request ever leaves
the process.
"""

from __future__ import annotations

import httpx
import pytest
import sso_client
import sso_window
from sso_client import (
    EXTRACTION_CACHE_DIR,
    HTML_CACHE_DIR,
    USER_AGENT,
    CircuitBreakerOpen,
    SSOClient,
    cache_write,
)


@pytest.fixture(autouse=True)
def offline_env(monkeypatch):
    monkeypatch.setenv("STATUTES_IGNORE_WINDOW", "1")
    monkeypatch.setenv("STATUTES_NO_DELAY", "1")


def make_client(handler, **kwargs) -> SSOClient:
    return SSOClient(transport=httpx.MockTransport(handler), **kwargs)


# =============================================================================
# get() — success path
# =============================================================================
def test_get_success_returns_response_and_sends_user_agent():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="<html>ok</html>")

    with make_client(handler) as client:
        response = client.get("/Act/IA1965")

    assert response.status_code == 200
    assert response.text == "<html>ok</html>"
    assert len(seen) == 1
    assert seen[0].headers["user-agent"] == USER_AGENT
    assert str(seen[0].url) == "https://sso.agc.gov.sg/Act/IA1965"
    assert client.consecutive_failures == 0


def test_get_passes_params():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    with make_client(handler) as client:
        client.get("/Browse/Act/Current/All", params={"PageSize": "500", "SortBy": "Title"})

    assert seen[0].url.params["PageSize"] == "500"
    assert seen[0].url.params["SortBy"] == "Title"


def test_get_rejects_non_sso_urls():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200)

    with make_client(handler) as client:
        with pytest.raises(ValueError):
            client.get("https://example.com/anything")


# =============================================================================
# get() — retry behaviour
# =============================================================================
def test_get_retries_5xx_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500)
        return httpx.Response(200, text="recovered")

    with make_client(handler) as client:
        response = client.get("/Browse/Act/Current/All")

    assert response.text == "recovered"
    assert calls["n"] == 3  # two failed attempts + one success, all in one get()
    assert client.consecutive_failures == 0


def test_get_does_not_retry_4xx():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    with make_client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.get("/Act/NOPE")

    assert calls["n"] == 1
    assert client.consecutive_failures == 1


# =============================================================================
# get() — circuit breaker
# =============================================================================
def test_circuit_breaker_opens_after_five_consecutive_failures():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    with make_client(handler) as client:
        for _ in range(4):
            with pytest.raises(httpx.HTTPStatusError):
                client.get("/Act/IA1965")

        # The fifth consecutive failure trips the breaker.
        with pytest.raises(CircuitBreakerOpen):
            client.get("/Act/IA1965")
        transport_calls_when_opened = calls["n"]
        assert transport_calls_when_opened == 5 * 3  # 3 tenacity attempts per get()

        # Once open, no new requests reach the transport at all.
        with pytest.raises(CircuitBreakerOpen):
            client.get("/Act/IA1965")

    assert calls["n"] == transport_calls_when_opened


def test_success_resets_consecutive_failure_counter():
    state = {"fail": True, "n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(500 if state["fail"] else 200, text="ok")

    with make_client(handler) as client:
        for _ in range(4):
            with pytest.raises(httpx.HTTPStatusError):
                client.get("/Act/IA1965")
        assert client.consecutive_failures == 4

        state["fail"] = False
        client.get("/Act/IA1965")
        assert client.consecutive_failures == 0

        # The breaker did not open: four more failures are tolerated again.
        state["fail"] = True
        with pytest.raises(httpx.HTTPStatusError):
            client.get("/Act/IA1965")
        assert client.consecutive_failures == 1


# =============================================================================
# get() — ToU window guard
# =============================================================================
def test_window_guard_runs_before_any_request(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls["n"] += 1
        return httpx.Response(200)

    def deny() -> None:
        raise sso_window.WindowClosedError("outside 03:00-07:00 SGT")

    monkeypatch.setattr(sso_window, "require_window", deny)

    with make_client(handler) as client:
        with pytest.raises(sso_window.WindowClosedError):
            client.get("/Act/IA1965")

    assert calls["n"] == 0


# =============================================================================
# cache_write + cache dir constants
# =============================================================================
def test_cache_write_creates_parents_and_leaves_no_tmp(tmp_path):
    target = tmp_path / "statutes_html" / "deadbeef0123.html"
    cache_write(target, "<html>full body</html>")

    assert target.read_text(encoding="utf-8") == "<html>full body</html>"
    assert list(tmp_path.rglob("*.tmp")) == []


def test_cache_write_overwrites_atomically(tmp_path):
    target = tmp_path / "extraction.json"
    cache_write(target, '{"v": 1}')
    cache_write(target, '{"v": 2}')

    assert target.read_text(encoding="utf-8") == '{"v": 2}'
    assert list(tmp_path.rglob("*.tmp")) == []


def test_module_constants_pin_the_shared_contract():
    assert sso_client.BASE_URL == "https://sso.agc.gov.sg"
    assert USER_AGENT == "ZeekerBot/1.0 (+https://data.zeeker.sg)"
    assert str(HTML_CACHE_DIR) == ".cache/statutes_html"
    assert str(EXTRACTION_CACHE_DIR) == ".cache/statutes_extractions"


def test_get_xhr_sends_xmlhttprequest_header_only_when_asked():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    with make_client(handler) as client:
        client.get("/Details/GetLazyLoadContent", xhr=True)
        client.get("/Act/IA1965")

    assert seen[0].headers["x-requested-with"] == "XMLHttpRequest"
    assert seen[0].headers["user-agent"] == USER_AGENT  # UA stays honest on XHRs
    assert "x-requested-with" not in seen[1].headers


# =============================================================================
# Retry pacing — backoff waits must never undercut the polite delay (ToU)
# =============================================================================
def test_retry_wait_floor_is_the_polite_delay(monkeypatch):
    """robots.txt declares crawl-delay 6; a retry is still a request, so the
    configured exponential backoff's minimum wait must be >= the delay."""
    from tenacity import wait_exponential

    monkeypatch.delenv("STATUTES_NO_DELAY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    with make_client(handler) as client:
        wait = client._retrying.wait
        assert isinstance(wait, wait_exponential)
        assert wait.min >= client.delay_seconds >= 6.0
        assert wait.max >= wait.min

    # The floor follows a delay override upward too.
    with make_client(handler, delay_seconds=9.0) as client:
        assert client._retrying.wait.min >= 9.0
        assert client._retrying.wait.max >= client._retrying.wait.min


def test_retry_wait_is_zero_on_the_no_delay_fast_path():
    from tenacity import wait_none

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    with make_client(handler) as client:  # offline_env sets STATUTES_NO_DELAY=1
        assert isinstance(client._retrying.wait, wait_none)
