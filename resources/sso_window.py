"""
SSO Terms-of-Use extraction-window guard.

Singapore Statutes Online's Terms of Use, clause 13(d)(i)
(https://sso.agc.gov.sg/Terms-of-Use, "Use/Reproduction of Singapore
legislation"), permits automated extraction from SSO only between
3 a.m. and 7 a.m. Singapore Time. This module is the runtime half of a
two-layer enforcement scheme:

1. **Scheduler layer**: the personal agent that invokes ``scripts/nightly.sh``
   sets its timer to start runs shortly after 03:00 SGT — so runs *start* in
   the window by construction.
2. **Runtime layer (this module)**: because timers drift, runs can be
   triggered manually, and local runs exist, every live request to
   sso.agc.gov.sg must also pass this guard at request time.
   ``SSOClient.get()`` calls :func:`require_window` before EVERY request,
   so a run that drifts past the window checkpoints and exits cleanly
   instead of issuing out-of-window traffic.

The guard enforces two boundaries:

- **Window**: requests are only allowed 03:00 <= t < 07:00 SGT.
- **Hard stop**: no NEW requests after 06:45 SGT, leaving a 15-minute
  margin so in-flight work (retries, parsing, DB writes) finishes inside
  the window.

Escape hatch: setting the environment variable ``STATUTES_IGNORE_WINDOW=1``
makes :func:`window_allows_request` and :func:`require_window` pass
unconditionally. This is ONLY for fixture and local-file work (tests,
parsing cached HTML, dry runs against committed fixtures) — it must NEVER
be used to fetch from the live site outside the window.

Naive datetimes: any ``dt`` passed to the predicates must be timezone-aware.
Naive datetimes are ambiguous (local time? UTC? SGT?) and a wrong guess here
is a Terms-of-Use violation, so they are rejected with ``ValueError`` rather
than silently interpreted.

Pure stdlib (zoneinfo, datetime, os) — no third-party dependencies, so the
guard is importable from anywhere (scripts, tests, CI) without environment
setup.
"""

from __future__ import annotations

import os
from datetime import datetime, time
from zoneinfo import ZoneInfo

SGT = ZoneInfo("Asia/Singapore")

#: Extraction window per SSO Terms of Use cl 13(d)(i): 03:00–07:00 SGT.
WINDOW_OPEN = time(3, 0)
WINDOW_CLOSE = time(7, 0)

#: No NEW requests at or after this time, so in-flight work completes
#: inside the window.
HARD_STOP = time(6, 45)

#: Env var that bypasses the guard. Fixture/local-file work ONLY.
IGNORE_WINDOW_ENV = "STATUTES_IGNORE_WINDOW"


class WindowClosedError(RuntimeError):
    """Raised when a live SSO request is attempted outside the permitted window."""


def now_sgt() -> datetime:
    """Return the current time as a tz-aware datetime in Singapore Time."""
    return datetime.now(tz=SGT)


def _as_sgt(dt: datetime | None) -> datetime:
    """Normalise ``dt`` to SGT; default to now. Naive datetimes are rejected."""
    if dt is None:
        return now_sgt()
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(
            "naive datetime passed to window guard; pass a tz-aware datetime "
            "(the guard refuses to guess a timezone for a Terms-of-Use check)"
        )
    return dt.astimezone(SGT)


def in_window(dt: datetime | None = None) -> bool:
    """True if ``dt`` (default: now) falls inside 03:00 <= t < 07:00 SGT."""
    t = _as_sgt(dt).timetz().replace(tzinfo=None)
    return WINDOW_OPEN <= t < WINDOW_CLOSE


def hard_stop_reached(dt: datetime | None = None) -> bool:
    """True if NEW requests must not be issued at ``dt`` (default: now).

    The hard stop is reached at or after 06:45 SGT, and trivially whenever
    ``dt`` is outside the window altogether.
    """
    sgt = _as_sgt(dt)
    if not in_window(sgt):
        return True
    return sgt.timetz().replace(tzinfo=None) >= HARD_STOP


def window_allows_request(dt: datetime | None = None) -> bool:
    """True if a new live request to SSO may be issued at ``dt`` (default: now).

    Equivalent to ``in_window(dt) and not hard_stop_reached(dt)``.

    Returns True unconditionally when the environment variable
    ``STATUTES_IGNORE_WINDOW=1`` is set — ONLY for fixture/local-file work,
    never against the live site.
    """
    if os.environ.get(IGNORE_WINDOW_ENV) == "1":
        return True
    return in_window(dt) and not hard_stop_reached(dt)


def require_window() -> None:
    """Raise :class:`WindowClosedError` unless a new live request is allowed now.

    Called by ``SSOClient.get()`` before every request. Honours the
    ``STATUTES_IGNORE_WINDOW=1`` escape hatch via
    :func:`window_allows_request` (fixture/local-file work only).
    """
    if not window_allows_request():
        raise WindowClosedError(
            f"Outside SSO extraction window (now {now_sgt():%H:%M:%S} SGT). "
            f"Live requests are permitted only {WINDOW_OPEN:%H:%M}-{WINDOW_CLOSE:%H:%M} SGT "
            f"with a {HARD_STOP:%H:%M} hard stop for new requests "
            "(SSO Terms of Use cl 13(d)(i)). "
            "Set STATUTES_IGNORE_WINDOW=1 for fixture/local-file work only."
        )
