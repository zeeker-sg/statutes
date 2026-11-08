"""Gazette stream resource: the append-only as-published event log (PRD).

Nightly stream ingestion (PRD "Rolling update architecture", step 1): poll
the current year's SL-Supp and Acts-Supp browse pages (one request per
series per night; in January the previous year is polled too, to catch the
cursor rollover), parse them with ``parse_supp``, and insert every item
whose ``number_seq`` is above the per-``(series, year)`` high-water mark.
The cursor is derived from the DB itself
(``SELECT MAX(number_seq) ... WHERE series=? AND year=?``) — no checkpoint
files (PRD "State management in ephemeral CI").

Event interpretation, with the hard rule that **stream events never create
catalog rows**:

- ``amendment`` / ``revocation`` events carry a parsed ``principal_title``;
  when exactly one catalog Act matches it (title compared year-insensitively),
  the row is resolved (``principal_id``/``principal_resolved``) and the
  principal is **invalidated** (``content_text = NULL``) so the next acts
  queue drain re-extracts its consolidated text.
- Ambiguous/unmatched principals stay ``principal_resolved = false`` — the
  catalog-reconciliation safety net (listing_stamp diff) catches the
  consolidation anyway. SL principals stay unresolved until the SL catalog
  ships (Phase 5); the rows wait.
- Late resolution of old unresolved rows is deliberately NOT attempted:
  re-resolving long after gazettal would re-invalidate works whose
  consolidation the listing diff already handled.

Ordering note: zeeker runs this resource FIRST (zeeker.toml order), per the
PRD's rolling-update sequence — stream ingestion, then catalog
reconciliation, then queue drain — so a principal invalidated tonight is
re-extracted by ``acts`` in the SAME night's queue drain. ``act_versions``
runs after ``acts`` because it reads the extraction cache ``acts`` writes.
This resource also runs closest to the 06:45 SGT hard stop on heavy nights
only if the window has already closed before the build reaches it — either
way ``WindowClosedError`` / ``CircuitBreakerOpen`` are caught and the run
ends cleanly with whatever rows were ingested (the monotonic cursor picks
up the rest tomorrow).

Testing hook: ``TRANSPORT`` (module global) accepts an
``httpx.MockTransport``; always ``None`` in production.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
import httpx
from sqlite_utils.db import Table

# Zeeker loads resource files via importlib.util.spec_from_file_location,
# which bypasses package imports — ``from resources import ...`` fails at
# build time. Make sibling modules importable by adding this file's
# directory to sys.path before importing them.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_state  # noqa: E402
import ids  # noqa: E402
import parse_supp  # noqa: E402
import sso_client  # noqa: E402
import sso_window  # noqa: E402

#: Pre-created schema: zeeker only creates the table from returned rows AFTER
#: fetch_data, so a from-scratch first build would otherwise end without the
#: unique-index tripwire (PRD "Identity and keys").
STREAM_SCHEMA: Dict[str, type] = {
    "id": str,
    "series": str,
    "number": str,
    "number_seq": int,
    "year": int,
    "title": str,
    "published_date": str,
    "source_url": str,
    "pdf_url": str,
    "event_kind": str,
    "principal_title": str,
    "principal_id": str,
    "principal_resolved": bool,
    "created_at": str,
}

SUPP_PATHS = {
    "sl_supp": "/Browse/SL-Supp/Published/{year}",
    "acts_supp": "/Browse/Acts-Supp/Published/{year}",
}
#: Nightly poll: newest first so page 1 always contains everything above the
#: cursor (PRD: one request per stream per night).
POLL_PARAMS = {"SortBy": "Number", "SortOrder": "DESC", "PageSize": 500}

_TRAILING_YEAR_RE = re.compile(r"\s+\d{4}$")

#: Test seam: integration tests inject an ``httpx.MockTransport`` here.
TRANSPORT: Optional[httpx.BaseTransport] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(db: Any) -> None:
    """Create the table and unique-index tripwire up front (idempotent)."""
    db["gazette_stream"].create(STREAM_SCHEMA, if_not_exists=True)
    ensure_indexes(db)


def ensure_indexes(db: Any) -> None:
    """Unique-index tripwire on the natural identifier (PRD)."""
    if db["gazette_stream"].exists():
        db["gazette_stream"].create_index(
            ["series", "year", "number_seq"], unique=True, if_not_exists=True
        )


def _poll_years() -> List[int]:
    """Current SGT year; around New Year, the previous year too (PRD)."""
    now = sso_window.now_sgt()
    return [now.year, now.year - 1] if now.month == 1 else [now.year]


def _cursor(existing_table: Optional[Table], series: str, year: int) -> int:
    if existing_table is None:
        return 0
    row = existing_table.db.execute(
        "SELECT MAX(number_seq) FROM gazette_stream WHERE series = ? AND year = ?",
        [series, year],
    ).fetchone()
    return row[0] or 0


# ---------------------------------------------------------------------------
# Principal resolution (title-based, deliberately conservative)
# ---------------------------------------------------------------------------
def _normalize_title(title: str) -> str:
    """Year-insensitive comparison key: trailing year off, whitespace folded."""
    return re.sub(r"\s+", " ", _TRAILING_YEAR_RE.sub("", title)).strip().casefold()


def _build_resolver(db: Any) -> Dict[str, Optional[str]]:
    """Map of normalized catalog title -> act id (None when ambiguous)."""
    resolver: Dict[str, Optional[str]] = {}
    if not db["acts"].exists():
        return resolver
    for row in db.execute("SELECT id, title FROM acts").fetchall():
        key = _normalize_title(row[1])
        resolver[key] = None if key in resolver else row[0]
    return resolver


def _invalidate_principal(db: Any, act_id: str, number: str) -> None:
    click.echo(f"gazette_stream: invalidating principal {act_id} (amended by {number})")
    db.execute(
        "UPDATE [acts] SET [content_text] = NULL, [has_content] = 0 WHERE id = ?",
        [act_id],
    )


# ---------------------------------------------------------------------------
# Zeeker entry point
# ---------------------------------------------------------------------------
def fetch_data(existing_table: Optional[Table]) -> List[Dict[str, Any]]:
    """Poll the supplement streams; return rows above each (series, year) cursor.

    A closed extraction window or an open circuit breaker ends the poll
    cleanly: whatever rows were ingested before the stop are still returned
    (the per-(series, year) cursor is monotonic, so the next nightly run
    picks up exactly where this one stopped).
    """
    db = existing_table.db if existing_table is not None else build_state.connect_db()
    ensure_schema(db)

    new_rows: List[Dict[str, Any]] = []
    now = _now_iso()
    with sso_client.SSOClient(transport=TRANSPORT) as client:
        try:
            for series in parse_supp.SERIES:
                for year in _poll_years():
                    response = client.get(SUPP_PATHS[series].format(year=year), params=POLL_PARAMS)
                    items = parse_supp.parse_supp_page(response.text, series)
                    cursor = _cursor(existing_table, series, year)
                    fresh = sorted(
                        (i for i in items if i["year"] == year and i["number_seq"] > cursor),
                        key=lambda item: item["number_seq"],
                    )
                    click.echo(
                        f"gazette_stream: {series}/{year} cursor {cursor} — "
                        f"{len(fresh)} new of {len(items)} listed"
                    )
                    for item in fresh:
                        new_rows.append(
                            {
                                "id": ids.gazette_id(series, item["year"], item["number_seq"]),
                                "series": item["series"],
                                "number": item["number"],
                                "number_seq": item["number_seq"],
                                "year": item["year"],
                                "title": item["title"],
                                "published_date": item["published_date"],
                                "source_url": item["source_url"],
                                "pdf_url": item["pdf_url"],
                                "event_kind": item["event_kind"],
                                "principal_title": item["principal_title"],
                                "principal_id": None,
                                "principal_resolved": False,
                                "created_at": now,
                            }
                        )
        except (sso_window.WindowClosedError, sso_client.CircuitBreakerOpen) as exc:
            click.echo(
                f"gazette_stream: stopping poll cleanly ({exc}) — "
                f"keeping {len(new_rows)} row(s) ingested so far"
            )

    # Resolve and invalidate principals for tonight's amendment/revocation
    # events (needs the catalog, hence a DB handle; on the very first build
    # the rows simply wait for the reconciliation safety net).
    if db is not None:
        resolver = _build_resolver(db)
        for row in new_rows:
            if row["event_kind"] not in ("amendment", "revocation"):
                continue
            if not row["principal_title"]:
                continue
            act_id = resolver.get(_normalize_title(row["principal_title"]))
            if act_id is None:
                click.echo(
                    f"gazette_stream: principal {row['principal_title']!r} unresolved "
                    f"for {row['number']} — reconciliation safety net will catch it"
                )
                continue
            row["principal_id"] = act_id
            row["principal_resolved"] = True
            _invalidate_principal(db, act_id, row["number"])

    return new_rows
