"""Act version-timeline resource (PRD ``act_versions`` — metadata, no text).

One row per point-in-time expression of each Act: validity date, document
date, amending instrument (or revised-edition marker), historical/PDF URLs,
and an ``is_current`` flag. The table is **append-mostly**: each run appends
the timeline rows it has not seen; existing rows are never deleted, and only
the ``is_current`` flag is updated in place when the current expression
moves forward.

Zero extra requests: timelines ride on the detail pages the ``acts``
resource already fetched this build. This module reads the extraction-cache
records ``acts.fetch_data`` wrote (``versions`` parsed by
``parse_detail``) — zeeker runs resources in zeeker.toml order, so the
cache is populated by the time this resource runs. A build that extracted
nothing appends nothing (the no-op invariant).

Identity (PRD "Identity and keys"): ``id`` = hash of
``{act_id}:{valid_date}`` — one expression per work per validity date —
with a unique-index tripwire on ``(act_id, valid_date)``.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
from sqlite_utils.db import Table

# Zeeker loads resource files via importlib.util.spec_from_file_location,
# which bypasses package imports — ``from resources import ...`` fails at
# build time. Make sibling modules importable by adding this file's
# directory to sys.path before importing them.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_state  # noqa: E402
import ids  # noqa: E402
import sso_client  # noqa: E402

#: Pre-created schema: zeeker only creates the table from returned rows AFTER
#: fetch_data, so a from-scratch first build would otherwise end without the
#: unique-index tripwire (PRD "Identity and keys").
VERSIONS_SCHEMA: Dict[str, type] = {
    "id": str,
    "act_id": str,
    "shorthand": str,
    "valid_date": str,
    "doc_date": str,
    "amended_by": str,
    "historical_url": str,
    "pdf_url": str,
    "is_current": bool,
    "created_at": str,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(db: Any) -> None:
    """Create the table and unique-index tripwire up front (idempotent)."""
    db["act_versions"].create(VERSIONS_SCHEMA, if_not_exists=True)
    ensure_indexes(db)


def ensure_indexes(db: Any) -> None:
    """Unique-index tripwire on the natural identifier (PRD)."""
    if db["act_versions"].exists():
        db["act_versions"].create_index(["act_id", "valid_date"], unique=True, if_not_exists=True)


def fetch_data(existing_table: Optional[Table]) -> List[Dict[str, Any]]:
    """Append timeline rows from this build's extraction cache.

    Returns only rows whose ``(act_id, valid_date)`` identity is new;
    ``is_current`` drift on known rows is updated in place.
    """
    db = existing_table.db if existing_table is not None else build_state.connect_db()
    ensure_schema(db)

    existing_current: Dict[str, int] = {}
    if existing_table is not None:
        existing_current = {
            row["id"]: row["is_current"]
            for row in existing_table.rows_where(select="id, is_current")
        }

    rows: List[Dict[str, Any]] = []
    now = _now_iso()
    for path in sorted(sso_client.EXTRACTION_CACHE_DIR.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        act_id = record["act_id"]
        for version in record["versions"]:
            version_id = ids.version_id(act_id, version["valid_date"])
            if version_id in existing_current:
                if bool(existing_current[version_id]) != bool(version["is_current"]):
                    # The current expression moved forward: flip the flag.
                    existing_table.db.execute(
                        "UPDATE [act_versions] SET [is_current] = ? WHERE id = ?",
                        [version["is_current"], version_id],
                    )
                continue
            rows.append(
                {
                    "id": version_id,
                    "act_id": act_id,
                    "shorthand": record["shorthand"],
                    "valid_date": version["valid_date"],
                    "doc_date": version["doc_date"],
                    "amended_by": version["amended_by"],
                    "historical_url": version["historical_url"],
                    "pdf_url": version["pdf_url"],
                    "is_current": version["is_current"],
                    "created_at": now,
                }
            )
    click.echo(f"act_versions: {len(rows)} new timeline row(s)")
    return rows
