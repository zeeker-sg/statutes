"""Polite HTTP client and disk cache for Singapore Statutes Online (SSO).

Source:   https://sso.agc.gov.sg
ToU:      Automated extraction is only permitted 03:00-07:00 SGT (Terms of
          Use cl 13(d)(i)). ``SSOClient.get`` calls
          ``sso_window.require_window()`` before EVERY request, so a client
          held across a long extraction loop exits cleanly mid-batch instead
          of overrunning the window. The UA is the honest ZeekerBot string
          (cl 13(c)/(d): no browser impersonation, no anti-bot evasion).

Crawl strategy (shared by every statutes resource)
--------------------------------------------------
- Single ``httpx.Client`` with connection pool, jittered 6s+0-1s delay (robots.txt crawl-delay: 6)
  before each request, 3-attempt exponential-backoff retry on transport
  errors and 5xx whose waits are floored at the polite delay — a retry is
  still a request, so it must never beat the crawl-delay (4xx surfaces
  immediately — a blocked route is a dropped route, never retried into),
  and a circuit breaker that raises
  ``CircuitBreakerOpen`` after 5 consecutive failed requests so a sick
  source ends the run instead of burning the extraction window.

Disk cache
----------
``HTML_CACHE_DIR`` (raw detail-page HTML) and ``EXTRACTION_CACHE_DIR``
(parsed output) live under ``.cache/`` (gitignored). The extraction cache
is the source of truth that ``fetch_fragments_data`` reads from — zeeker
reloads resource modules between the main-table and fragments phases, so
any in-memory state would be lost. Disk cache survives the reload and any
crash mid-run: all writes go through ``cache_write`` (tmp file +
``os.replace``) so a SIGINT can't leave a half-written file behind. Per the
PRD's state-management rules the cache only needs to survive within a
single build — ephemeral CI disk is fine.

Env knobs
---------
- ``STATUTES_DELAY_SECONDS`` / ``STATUTES_JITTER_SECONDS`` — pacing
  overrides (defaults 6.0 / 1.0; do not go below robots.txt crawl-delay).
- ``STATUTES_NO_DELAY=1`` — skip pacing and backoff sleeps. Tests against
  ``httpx.MockTransport`` / cached fixtures only; never against the live
  site.
- ``STATUTES_IGNORE_WINDOW=1`` — honoured by ``sso_window``; fixture work
  against cached/local files only, never against the live site.
"""

from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urljoin

import httpx
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_none,
)

# Zeeker loads resource files via importlib.util.spec_from_file_location,
# which bypasses package imports — ``from resources import ...`` fails at
# build time. Make sibling modules importable by adding this file's
# directory to sys.path before importing them.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sso_window  # noqa: E402

# =============================================================================
# CONFIGURATION
# =============================================================================
BASE_URL = "https://sso.agc.gov.sg"
USER_AGENT = "ZeekerBot/1.0 (+https://data.zeeker.sg)"

REQUEST_TIMEOUT = 30.0
MAX_RETRIES = 3

# Crawl pacing (env-overridable so smoke tests don't require code edits).
# sso.agc.gov.sg/robots.txt declares "crawl-delay: 6" (verified 2026-06-11),
# so the base delay is 6s and jitter is purely additive — the gap between
# requests must never dip below the declared crawl-delay.
DELAY_SECONDS = float(os.environ.get("STATUTES_DELAY_SECONDS", "6.0"))
JITTER_SECONDS = float(os.environ.get("STATUTES_JITTER_SECONDS", "1.0"))

# Circuit breaker
MAX_CONSECUTIVE_FAILURES = 5

# Disk cache roots (see module docstring — bridges zeeker's module reload
# between the main-table and fragments phases)
HTML_CACHE_DIR = Path(".cache/statutes_html")
EXTRACTION_CACHE_DIR = Path(".cache/statutes_extractions")

# Retry transport errors (DNS, timeouts, resets) and 5xx; 4xx never retries.
_RETRYABLE_EXCEPTIONS = (httpx.TransportError, httpx.HTTPStatusError)


def _no_delay() -> bool:
    return os.environ.get("STATUTES_NO_DELAY") == "1"


class CircuitBreakerOpen(Exception):
    """Too many consecutive request failures — stop hitting the source."""


def cache_write(path: Path, content: str) -> None:
    """Atomically write ``content`` to ``path``, creating parent dirs.

    Writes to a sibling ``.tmp`` file then ``os.replace``s it into place so
    a crash or SIGINT mid-write can never leave a half-written cache file
    behind — readers either see the old complete file or the new one.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


class SSOClient:
    """Polite, window-guarded HTTP client for sso.agc.gov.sg.

    Every ``get`` checks the ToU extraction window, applies the jittered
    polite delay, retries transient failures with exponential backoff, and
    feeds a consecutive-failure circuit breaker. The client refuses URLs
    outside ``BASE_URL`` — it exists to talk to SSO and nothing else.
    """

    def __init__(
        self,
        *,
        delay_seconds: float = DELAY_SECONDS,
        jitter_seconds: float = JITTER_SECONDS,
        max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.delay_seconds = delay_seconds
        self.jitter_seconds = jitter_seconds
        self.max_consecutive_failures = max_consecutive_failures
        self.consecutive_failures = 0
        self._client = httpx.Client(
            timeout=REQUEST_TIMEOUT,
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=3),
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            follow_redirects=True,
            transport=transport,
        )
        # Retry waits must never undercut the polite delay: robots.txt
        # declares crawl-delay 6, and a retry is still a request, so the
        # backoff floor is the configured delay (min >= delay_seconds). The
        # NO_DELAY=1 fast path (fixtures/tests only) keeps waits at zero.
        self._retrying = Retrying(
            stop=stop_after_attempt(MAX_RETRIES),
            wait=(
                wait_none()
                if _no_delay()
                else wait_exponential(
                    multiplier=2,
                    min=max(1.0, self.delay_seconds),
                    max=max(10.0, self.delay_seconds * 2),
                )
            ),
            retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
            reraise=True,
        )

    # -- request plumbing -----------------------------------------------------
    def _resolve_url(self, url_or_path: str) -> str:
        if url_or_path.startswith(("http://", "https://")):
            if not url_or_path.startswith(BASE_URL):
                raise ValueError(f"SSOClient only talks to {BASE_URL}; got {url_or_path!r}")
            return url_or_path
        return urljoin(BASE_URL, url_or_path)

    def _polite_sleep(self) -> None:
        if _no_delay():
            return
        # Additive-only jitter: never sleep less than the base delay, which is
        # pinned to robots.txt's crawl-delay.
        delay = self.delay_seconds + random.uniform(0, self.jitter_seconds)
        time.sleep(max(0.1, delay))

    def _request_once(
        self,
        url: str,
        params: Optional[Dict[str, Any]],
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        response = self._client.get(url, params=params, headers=headers)
        # Raise (→ retry) on 5xx; return 4xx so it surfaces without retry.
        if 500 <= response.status_code < 600:
            response.raise_for_status()
        return response

    # -- public API -----------------------------------------------------------
    def get(
        self,
        url_or_path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        xhr: bool = False,
    ) -> httpx.Response:
        """GET an SSO URL or path, politely.

        Order of operations: (1) ToU window guard — raises
        ``sso_window.WindowClosedError`` outside 03:00-06:45 SGT; (2) circuit
        breaker — raises ``CircuitBreakerOpen`` if already open; (3) jittered
        polite delay; (4) GET with 3-attempt backoff on transport errors and
        5xx; (5) ``raise_for_status``. A success resets the consecutive-
        failure counter; the failure that reaches ``max_consecutive_failures``
        raises ``CircuitBreakerOpen`` instead of the underlying error.

        ``xhr=True`` adds ``X-Requested-With: XMLHttpRequest`` — the header
        the site's own JS sends on lazy-load calls (jQuery same-origin
        default; see tests/fixtures/lazyload_curl.txt). Used when replaying
        ``/Details/GetLazyLoadContent`` requests, openly, exactly as the
        page itself issues them.
        """
        sso_window.require_window()
        if self.consecutive_failures >= self.max_consecutive_failures:
            raise CircuitBreakerOpen(
                f"{self.consecutive_failures} consecutive failures — refusing new requests"
            )
        url = self._resolve_url(url_or_path)
        headers = {"X-Requested-With": "XMLHttpRequest"} if xhr else None
        self._polite_sleep()
        try:
            response = self._retrying(self._request_once, url, params, headers)
            response.raise_for_status()
        except Exception as exc:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.max_consecutive_failures:
                raise CircuitBreakerOpen(
                    f"{self.consecutive_failures} consecutive failures "
                    f"(last: {type(exc).__name__}: {exc})"
                ) from exc
            raise
        self.consecutive_failures = 0
        return response

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SSOClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
