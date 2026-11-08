"""Per-build state shared by the statutes resources (DB handle + build marker).

Two concerns live here, both consequences of how ``zeeker build`` drives
resource modules (see zeeker.core.database.builder/processor):

1. **Build-DB access for schema pre-creation.** Zeeker passes
   ``fetch_data(existing_table=None)`` on a from-scratch build and only
   creates the table from the returned rows *after* the resource runs — so a
   resource that wants its unique-index tripwires present at the end of the
   very first build (PRD "Identity and keys"; the S3 DB is the only durable
   state, so every night is potentially a "first build") must create the
   table + indexes itself, up front, so zeeker's ``insert_all`` lands into
   the prepared table. :func:`connect_db` opens the build database the same
   way zeeker does: the ``[project] database`` file named in ``zeeker.toml``
   in the working directory (``zeeker build`` always runs from the project
   root), defaulting to ``statutes.db``.

2. **The per-build marker.** For fragments-enabled resources zeeker calls
   ``fetch_data`` a SECOND time (in a freshly reloaded module, so module
   globals do not survive) purely to obtain the ``main_data_context``
   argument for ``fetch_fragments_data`` — the second return value is never
   inserted. Without a guard that second call would re-crawl the listing and
   re-drain the extraction queue, doubling the night's request budget and
   immediately retrying just-failed works. The first ``fetch_data`` writes
   ``.cache/statutes_build_marker.json`` at the start of a real run; a
   *fresh* marker (< :data:`MARKER_FRESH_HOURS` old) tells the second call
   to skip crawling and extraction entirely. A stale marker never suppresses
   a real run: nightly runs are ~24h apart, ephemeral CI wipes ``.cache/``
   anyway, and the marker is refreshed at the start of every real run.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import sqlite_utils

# Zeeker loads resource files via importlib.util.spec_from_file_location,
# which bypasses package imports — ``from resources import ...`` fails at
# build time. Make sibling modules importable by adding this file's
# directory to sys.path before importing them.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sso_client  # noqa: E402

#: Marker written by the first ``fetch_data`` of a build; read by the second
#: (fragments-context) invocation. Lives under ``.cache/`` so ephemeral CI
#: disk wipes it between builds (PRD "State management in ephemeral CI").
MARKER_PATH = Path(".cache/statutes_build_marker.json")

#: A marker younger than this is "the same build" (zeeker's two fetch_data
#: calls are seconds apart; nightly builds are ~24h apart). Older or missing
#: markers mean a real run.
MARKER_FRESH_HOURS = 6.0

DEFAULT_DB_FILENAME = "statutes.db"


def build_db_path() -> Path:
    """Path of the database ``zeeker build`` is writing, per ``zeeker.toml``.

    ``zeeker build`` runs from the project root, so the working directory's
    ``zeeker.toml`` ``[project] database`` names the file. Falls back to
    ``statutes.db`` when no ``zeeker.toml`` is present (tests reproduce the
    build loop in a bare working directory).
    """
    toml_path = Path("zeeker.toml")
    if toml_path.exists():
        try:
            config = tomllib.loads(toml_path.read_text(encoding="utf-8"))
            database = (config.get("project") or {}).get("database")
            if database:
                return Path(database)
        except (OSError, tomllib.TOMLDecodeError):
            pass
    return Path(DEFAULT_DB_FILENAME)


def connect_db() -> sqlite_utils.Database:
    """Open the build database for schema pre-creation (see module docstring)."""
    return sqlite_utils.Database(str(build_db_path()))


# ---------------------------------------------------------------------------
# Per-build marker (fragments-phase fetch_data must be side-effect-free)
# ---------------------------------------------------------------------------
def write_marker(now: Optional[datetime] = None) -> None:
    """Record that a real run started now; refreshes any previous marker."""
    started_at = (now or datetime.now(timezone.utc)).isoformat()
    sso_client.cache_write(MARKER_PATH, json.dumps({"started_at": started_at}))


def marker_is_fresh(now: Optional[datetime] = None) -> bool:
    """True when a marker exists and is younger than :data:`MARKER_FRESH_HOURS`.

    A missing, unreadable, malformed, or future-dated marker reads as stale —
    stale always means "run normally", so corruption can only cost an extra
    crawl, never suppress a nightly run.
    """
    try:
        payload = json.loads(MARKER_PATH.read_text(encoding="utf-8"))
        started_at = datetime.fromisoformat(payload["started_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if started_at.tzinfo is None:
        return False
    age = ((now or datetime.now(timezone.utc)) - started_at).total_seconds()
    return 0 <= age < MARKER_FRESH_HOURS * 3600


def clear_marker() -> None:
    """Remove the marker (test/maintenance helper; a real run refreshes it)."""
    try:
        os.remove(MARKER_PATH)
    except FileNotFoundError:
        pass
