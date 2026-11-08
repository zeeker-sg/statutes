"""Acts catalog resource: discovery, reconciliation, and full-text extraction.

Implements the ``acts`` main table and the ``acts_fragments`` table (PRD
Phases 1-3). One nightly ``zeeker build`` run does, in order:

1. **Discovery / reconciliation** — crawl the 2-page Acts browse listing
   (PageSize=500), upsert new catalog rows, and diff ``listing_stamp``
   against stored values: a changed stamp invalidates the row
   (``content_text = NULL``) so the queue re-extracts it. Vanished
   shorthands are flagged ``status = 'delisted'`` — never deleted.
2. **Queue drain** — extract works with ``content_text IS NULL`` (new and
   invalidated first, in listing order), capped by
   ``STATUTES_EXTRACT_MAX_PER_RUN`` and hard-stopped by the ToU window
   guard inside ``SSOClient``. Per Act: fetch the detail page, parse the
   status banner / long title / timeline (parse_detail), replay the page's
   own lazy-load XHRs for the body series, assemble and parse provisions
   into fragments (parse_provisions), then write the row update and the
   extraction-cache record.

State rules (PRD "State management in ephemeral CI"): every cursor derives
from the DB — the extraction queue IS ``content_text IS NULL``, the listing
diff baseline IS the stored ``listing_stamp``. The ``.cache/`` extraction
cache only bridges zeeker's module reload between the main-table and
fragments phases within ONE build: ``fetch_fragments_data`` replays this
run's extractions from disk (delete-then-insert per re-extracted work, so
fragment replacement is wholesale and idempotent).

Zeeker mechanics encoded here: ``fetch_data`` returns ONLY new rows
(``insert_all`` without pk — duplicate avoidance is the resource's job);
existing rows are updated in place via SQL on ``existing_table.db``. Tables
and unique-index tripwires are pre-created via ``ensure_schema`` so even a
from-scratch first build ends with the tripwires present. Zeeker calls
``fetch_data`` a SECOND time during the fragments phase (module reloaded in
between) just to build ``main_data_context``; the per-build marker in
``build_state`` makes that call request-free and side-effect-free.
Failures follow the quarantine pattern: a failed extraction is logged and
skipped (re-queued naturally next run because its ``content_text`` stays
NULL) — and an incomplete body (missing/empty lazy-load series) IS a
failure, never a partial extraction (ToU cl 13(b));
``WindowClosedError`` / ``CircuitBreakerOpen`` end the run cleanly.

Testing hook: ``TRANSPORT`` (module global) accepts an
``httpx.MockTransport`` so the offline integration test can run the full
pipeline against committed fixtures. Always ``None`` in production.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
import parse_detail  # noqa: E402
import parse_listing  # noqa: E402
import parse_provisions  # noqa: E402
import sso_client  # noqa: E402
import sso_window  # noqa: E402

LISTING_URL = "/Browse/Act/Current/All?PageSize=500&SortBy=Title&SortOrder=ASC"

#: Test seam: integration tests inject an ``httpx.MockTransport`` here.
TRANSPORT: Optional[httpx.BaseTransport] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_enabled() -> bool:
    return os.environ.get("STATUTES_EXTRACT_ENABLED", "1") != "0"


def _max_per_run() -> int:
    return int(os.environ.get("STATUTES_EXTRACT_MAX_PER_RUN", "40"))


def update_by_id(db: Any, table: str, row_id: str, updates: Dict[str, Any]) -> None:
    """UPDATE one row by its ``id`` column.

    Zeeker inserts without a declared primary key (``insert_all`` with no
    ``pk=``), so ``Table.update`` (rowid-based) cannot address rows by our
    opaque ``id`` — plain SQL can.
    """
    assignments = ", ".join(f"[{column}] = ?" for column in updates)
    db.execute(
        f"UPDATE [{table}] SET {assignments} WHERE id = ?",
        [*updates.values(), row_id],
    )


class ExtractionError(RuntimeError):
    """A work's body cannot be fetched/assembled completely.

    Raised instead of emitting partial statutory text (ToU cl 13(b): we own
    accuracy). The drain quarantines the work — ``content_text`` stays NULL,
    no cache record is written — so it re-queues naturally next run.
    """


#: Pre-created table schemas (PRD "Identity and keys"): zeeker only creates
#: tables from returned rows AFTER fetch_data runs, so a from-scratch first
#: build would otherwise end without the unique-index tripwires. Creating the
#: table + indexes up front means zeeker's ``insert_all`` lands into the
#: prepared table and even the very first build deploys with tripwires intact.
ACTS_SCHEMA: Dict[str, type] = {
    "id": str,
    "shorthand": str,
    "title": str,
    "year": int,
    "status": str,
    "subtype": str,
    "current_as_at": str,
    "listing_stamp": str,
    "source_url": str,
    "pdf_url": str,
    "rss_url": str,
    "has_sl": bool,
    "long_title": str,
    "content_text": str,
    "has_content": bool,
    "fragment_count": int,
    "extracted_at": str,
    "created_at": str,
}

FRAGMENTS_SCHEMA: Dict[str, type] = {
    "id": str,
    "parent_id": str,
    "provision_id": str,
    "provision_num": str,
    "unit_kind": str,
    "heading": str,
    "part_num": str,
    "part_heading": str,
    "seq": int,
    "content_text": str,
    "structure_json": str,
    "source_notes": str,
}


def ensure_schema(db: Any) -> None:
    """Create this resource's tables and unique-index tripwires up front.

    Idempotent; called at the start of every fetch (self-connecting to the
    build DB when zeeker passes ``existing_table=None`` on a first build).
    A tripwire failure later means key derivation drifted — investigate the
    source, never relax.
    """
    db["acts"].create(ACTS_SCHEMA, if_not_exists=True)
    db["acts_fragments"].create(FRAGMENTS_SCHEMA, if_not_exists=True)
    ensure_indexes(db)


def ensure_indexes(db: Any) -> None:
    """Create this resource's unique-index tripwires (PRD "Identity and keys")."""
    if db["acts"].exists():
        db["acts"].create_index(["shorthand"], unique=True, if_not_exists=True)
    if db["acts_fragments"].exists():
        db["acts_fragments"].create_index(
            ["parent_id", "provision_id"], unique=True, if_not_exists=True
        )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def _crawl_listing(client: sso_client.SSOClient) -> List[Dict[str, Any]]:
    """All listing rows across the paginated browse (2 requests at 500/page)."""
    rows: List[Dict[str, Any]] = []
    url: Optional[str] = LISTING_URL
    while url is not None:
        response = client.get(url)
        rows.extend(parse_listing.parse_listing_page(response.text))
        url = parse_listing.next_page_url(response.text)
    return rows


def _new_catalog_row(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": ids.act_id(item["shorthand"]),
        "shorthand": item["shorthand"],
        "title": item["title"],
        "year": item["year"],
        "status": "current",
        "subtype": item["subtype"],
        "current_as_at": None,
        "listing_stamp": item["listing_stamp"],
        "source_url": item["source_url"],
        "pdf_url": item["pdf_url"],
        "rss_url": item["rss_url"],
        "has_sl": item["has_sl"],
        "long_title": None,
        "content_text": None,
        "has_content": False,
        "fragment_count": 0,
        "extracted_at": None,
        "created_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def _extract_act(client: sso_client.SSOClient, shorthand: str, source_url: str) -> Dict[str, Any]:
    """Fetch + parse one Act; returns the catalog-row updates.

    Side effects: archives raw HTML under ``HTML_CACHE_DIR`` and writes the
    extraction-cache record (fragments + timeline) that
    ``fetch_fragments_data`` and the ``act_versions`` resource read.
    """
    response = client.get(source_url)
    html = response.text
    sso_client.cache_write(sso_client.HTML_CACHE_DIR / f"{shorthand}.html", html)

    detail = parse_detail.parse_detail_page(html)
    series_html: Dict[str, str] = {}
    lazyload = detail["lazyload"]
    if lazyload is not None:
        for entry in lazyload["series"]:
            if not entry.get("frag_sys_id"):
                # A series we cannot fetch means the assembled body would be
                # missing statutory text. Never extract a partial Act
                # (ToU cl 13(b)); quarantine and retry next run.
                raise ExtractionError(
                    f"{shorthand} series {entry.get('series_id')!r} has no FragSysId "
                    "in the page's fragments map — body would be incomplete"
                )
            url = parse_detail.build_lazyload_url(lazyload, entry)
            series_html[entry["series_id"]] = client.get(url, xhr=True).text

    body_html = parse_provisions.assemble_body(html, series_html)
    fragments = parse_provisions.parse_provisions(body_html, shorthand)
    body_fragments = [f for f in fragments if f["unit_kind"] != "longtitle"]
    content_text = "\n\n".join(f["content_text"] for f in body_fragments)

    extracted_at = _now_iso()
    act_id = ids.act_id(shorthand)
    record = {
        "act_id": act_id,
        "shorthand": shorthand,
        "source_url": source_url,
        "extracted_at": extracted_at,
        "fragments": fragments,
        "versions": detail["versions"],
    }
    sso_client.cache_write(
        sso_client.EXTRACTION_CACHE_DIR / f"{shorthand}.json",
        json.dumps(record, ensure_ascii=False),
    )
    return {
        "long_title": detail["long_title"],
        "current_as_at": detail["current_as_at"],
        "content_text": content_text,
        "has_content": bool(content_text.strip()),
        "fragment_count": len(fragments),
        "extracted_at": extracted_at,
    }


def _drain_queue(
    client: sso_client.SSOClient,
    queue: List[Tuple[str, str, Optional[Dict[str, Any]]]],
    existing_table: Optional[Table],
) -> None:
    cap = _max_per_run()
    extracted = 0
    for shorthand, source_url, new_row in queue:
        if extracted >= cap:
            click.echo(f"acts: extraction cap {cap} reached — {len(queue) - extracted} queued")
            break
        try:
            updates = _extract_act(client, shorthand, source_url)
        except (sso_window.WindowClosedError, sso_client.CircuitBreakerOpen) as exc:
            click.echo(f"acts: stopping extraction cleanly ({exc})")
            break
        except Exception as exc:  # quarantine pattern: log, skip, retry next run
            click.echo(f"warn: extraction failed for {shorthand}: {type(exc).__name__}: {exc}")
            continue
        extracted += 1
        if new_row is not None:
            new_row.update(updates)
        elif existing_table is not None:
            update_by_id(existing_table.db, "acts", ids.act_id(shorthand), updates)
    click.echo(f"acts: extracted {extracted} of {len(queue)} queued work(s)")


# ---------------------------------------------------------------------------
# Zeeker entry points
# ---------------------------------------------------------------------------
def fetch_data(existing_table: Optional[Table]) -> List[Dict[str, Any]]:
    """Discovery + reconciliation + queue drain; returns NEW catalog rows only.

    Existing rows are updated in place (stamp changes, invalidation,
    extraction results); zeeker inserts whatever is returned.

    Zeeker calls this a SECOND time (in a reloaded module) during the
    fragments phase, purely to build the ``main_data_context`` argument of
    ``fetch_fragments_data`` — that return value is never inserted, and our
    ``fetch_fragments_data`` reads the disk extraction cache instead of the
    context. The per-build marker (see ``build_state``) makes that second
    invocation cheap and side-effect-free: a fresh marker means "same
    build", so we skip crawling AND extraction and return the empty context.
    A stale/missing marker (next nightly run, wiped CI disk) runs normally.
    """
    if build_state.marker_is_fresh():
        click.echo(
            "acts: fresh build marker — fragments-context invocation; "
            "skipping crawl and extraction (no requests issued)"
        )
        return []
    build_state.write_marker()  # refreshed at the start of every real run

    db = existing_table.db if existing_table is not None else build_state.connect_db()
    ensure_schema(db)

    existing: Dict[str, Dict[str, Any]] = {}
    if existing_table is not None:
        existing = {
            row["shorthand"]: row
            for row in existing_table.rows_where(
                select="id, shorthand, listing_stamp, content_text, status"
            )
        }

    new_rows: List[Dict[str, Any]] = []
    queue: List[Tuple[str, str, Optional[Dict[str, Any]]]] = []
    backfill: List[Tuple[str, str, Optional[Dict[str, Any]]]] = []
    seen: set[str] = set()

    with sso_client.SSOClient(transport=TRANSPORT) as client:
        listing = _crawl_listing(client)
        click.echo(f"acts: listing has {len(listing)} current Acts")

        for item in listing:
            shorthand = item["shorthand"]
            seen.add(shorthand)
            previous = existing.get(shorthand)
            if previous is None:
                row = _new_catalog_row(item)
                new_rows.append(row)
                queue.append((shorthand, item["source_url"], row))
                continue
            stamp = item["listing_stamp"]
            if stamp and stamp != previous["listing_stamp"]:
                # Changed stamp == AGC re-consolidated this Act: invalidate so
                # the queue re-extracts (PRD catalog reconciliation).
                click.echo(f"acts: listing_stamp changed for {shorthand} — invalidating")
                update_by_id(
                    existing_table.db,
                    "acts",
                    previous["id"],
                    {"listing_stamp": stamp, "content_text": None, "has_content": False},
                )
                queue.append((shorthand, item["source_url"], None))
            elif previous["content_text"] is None:
                backfill.append((shorthand, item["source_url"], None))

        # Vanished shorthands: repeal candidates — flag, never delete.
        for shorthand, previous in existing.items():
            if shorthand not in seen and previous["status"] != "delisted":
                click.echo(f"acts: {shorthand} vanished from the listing — flagging delisted")
                update_by_id(existing_table.db, "acts", previous["id"], {"status": "delisted"})

        if _extract_enabled():
            _drain_queue(client, queue + backfill, existing_table)
        else:
            click.echo("acts: extraction disabled (STATUTES_EXTRACT_ENABLED=0)")

    return new_rows


def fetch_fragments_data(
    existing_fragments_table: Optional[Table],
    main_data_context: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Replay this build's extraction cache into ``acts_fragments`` rows.

    Zeeker reloads the module between the main-table and fragments phases,
    so the disk cache — not ``main_data_context`` — is the source of truth.
    Re-extracted works have their old fragments deleted first (wholesale
    replacement per the PRD), making this idempotent within and across runs.
    """
    db = (
        existing_fragments_table.db
        if existing_fragments_table is not None
        else build_state.connect_db()
    )
    ensure_schema(db)

    rows: List[Dict[str, Any]] = []
    cache_files = sorted(sso_client.EXTRACTION_CACHE_DIR.glob("*.json"))
    for path in cache_files:
        record = json.loads(path.read_text(encoding="utf-8"))
        parent_id = record["act_id"]
        db["acts_fragments"].delete_where("parent_id = ?", [parent_id])
        for fragment in record["fragments"]:
            rows.append(
                {
                    "id": ids.fragment_id(parent_id, fragment["provision_id"]),
                    "parent_id": parent_id,
                    "provision_id": fragment["provision_id"],
                    "provision_num": fragment["provision_num"],
                    "unit_kind": fragment["unit_kind"],
                    "heading": fragment["heading"],
                    "part_num": fragment["part_num"],
                    "part_heading": fragment["part_heading"],
                    "seq": fragment["seq"],
                    "content_text": fragment["content_text"],
                    "structure_json": fragment["structure_json"],
                    "source_notes": fragment["source_notes"],
                }
            )
    click.echo(f"acts_fragments: {len(rows)} fragment(s) from {len(cache_files)} cached work(s)")
    return rows
