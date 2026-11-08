"""Shared pytest setup for the statutes resource modules.

Zeeker loads resource files via ``importlib.util.spec_from_file_location``,
so the modules import siblings by bare name after a ``sys.path`` insert.
Mirror that here so tests import ``sso_client`` / ``ids`` exactly the way
the build does.

``sso_window`` is developed in parallel as a sibling module. If its file has
not landed yet, install a contract-conformant stand-in into ``sys.modules``
so the client tests (which run with ``STATUTES_IGNORE_WINDOW=1``) can still
import ``sso_client``; the real module wins automatically as soon as
``resources/sso_window.py`` exists.
"""

from __future__ import annotations

import os
import sys
import types
from datetime import datetime
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

RESOURCES_DIR = Path(__file__).resolve().parent.parent / "resources"
if str(RESOURCES_DIR) not in sys.path:
    sys.path.insert(0, str(RESOURCES_DIR))


def _install_sso_window_stub() -> None:
    mod = types.ModuleType("sso_window")
    sgt = ZoneInfo("Asia/Singapore")

    class WindowClosedError(RuntimeError):
        pass

    def now_sgt() -> datetime:
        return datetime.now(sgt)

    def _sgt_time(dt: datetime | None) -> dt_time:
        return (dt if dt is not None else now_sgt()).astimezone(sgt).time()

    def in_window(dt: datetime | None = None) -> bool:
        return dt_time(3, 0) <= _sgt_time(dt) < dt_time(7, 0)

    def hard_stop_reached(dt: datetime | None = None) -> bool:
        return not in_window(dt) or _sgt_time(dt) >= dt_time(6, 45)

    def window_allows_request(dt: datetime | None = None) -> bool:
        if os.environ.get("STATUTES_IGNORE_WINDOW") == "1":
            return True
        return in_window(dt) and not hard_stop_reached(dt)

    def require_window() -> None:
        if not window_allows_request():
            raise WindowClosedError(
                "SSO extraction window (03:00-07:00 SGT) is closed "
                "or past the 06:45 SGT hard stop"
            )

    mod.SGT = sgt
    mod.WindowClosedError = WindowClosedError
    mod.now_sgt = now_sgt
    mod.in_window = in_window
    mod.hard_stop_reached = hard_stop_reached
    mod.window_allows_request = window_allows_request
    mod.require_window = require_window
    sys.modules["sso_window"] = mod


if not (RESOURCES_DIR / "sso_window.py").exists() and "sso_window" not in sys.modules:
    _install_sso_window_stub()


# The SSO page fixtures are working files kept out of the public repo (they
# reproduce legislation). Without them, skip the modules that parse them.
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
_FIXTURE_MODULES = [
    "test_parse_listing.py",
    "test_parse_detail.py",
    "test_parse_provisions.py",
    "test_parse_supp.py",
    "test_integration_offline.py",
]
collect_ignore = [] if any(FIXTURES_DIR.glob("*.html")) else _FIXTURE_MODULES
