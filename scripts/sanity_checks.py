#!/usr/bin/env python3
"""Post-build sanity checks for the statutes database (PRD "Ops hardening").

Run after ``zeeker build`` and before ``zeeker deploy``. Fails loudly (exit 1)
when an invariant is violated so a broken build never reaches S3:

1. Append-mostly invariant: per-table row counts never decrease versus the
   previously deployed database (catalog rows are flagged, never deleted).
   Compared against ``statutes.previous.db`` -- the snapshot of S3
   ``latest/statutes.db`` taken by the workflow before the build. When no
   previous database exists (first run, local runs) the comparison is
   skipped with a warning. ``acts_fragments`` is deliberately EXEMPT: the
   PRD pins wholesale delete-then-reinsert per re-extracted work, so a
   re-consolidation that merges or renumbers sections legitimately shrinks
   a work's fragment count. The real fragment protection is check 2's
   per-act real-row gate, not a global count.
2. Every ``acts`` row with ``has_content = 1`` has at least one REAL
   ``acts_fragments`` row (``parent_id = acts.id``) -- the denormalized
   ``fragment_count`` column is also checked, but actual rows are the gate.
3. Unique-index tripwires are intact (PRD "Identity and keys"): if key
   derivation ever drifts, the build must fail loudly instead of silently
   inserting near-duplicates. A failed tripwire is a signal to investigate
   the source, never to relax the index.

Checks degrade gracefully (warn, pass) when a table does not exist yet --
early phases build the tables incrementally.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import click

# Tables we assert over. FTS shadow tables and other zeeker internals are ignored.
KNOWN_TABLES = ("acts", "acts_fragments", "act_versions", "gazette_stream")

# Tables under the strict never-decrease invariant. acts_fragments is exempt:
# re-extraction replaces a work's fragments wholesale (delete-then-reinsert,
# PRD "Identity and keys"), so a re-consolidated Act with fewer sections
# legitimately shrinks the table. Its real protection is the per-act
# real-row gate in check_content_has_fragments.
NEVER_DECREASE_TABLES = ("acts", "act_versions", "gazette_stream")

# Required unique indexes on natural identifiers, per the PRD tripwire table.
# Column sets are compared order-insensitively: any unique index covering
# exactly these columns satisfies the constraint.
REQUIRED_UNIQUE_INDEXES: dict[str, tuple[frozenset[str], ...]] = {
    "acts": (frozenset({"shorthand"}),),
    "gazette_stream": (frozenset({"series", "year", "number_seq"}),),
    "act_versions": (frozenset({"act_id", "valid_date"}),),
    "acts_fragments": (frozenset({"parent_id", "provision_id"}),),
}


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def row_count(conn: sqlite3.Connection, table: str) -> int:
    # Table names come from module constants, never user input.
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def unique_index_column_sets(conn: sqlite3.Connection, table: str) -> list[frozenset[str]]:
    """Column sets of every unique index on *table*, including implicit
    PRIMARY KEY / UNIQUE-constraint autoindexes."""
    column_sets = []
    for index in conn.execute(f"PRAGMA index_list({table})"):
        # index row: (seq, name, unique, origin, partial)
        if not index[2]:
            continue
        columns = {
            info[2]
            for info in conn.execute(f"PRAGMA index_info({index[1]})")
            if info[2] is not None  # rowid / expression members have no name
        }
        if columns:
            column_sets.append(frozenset(columns))
    return column_sets


def check_row_counts(conn: sqlite3.Connection, previous_path: Path, violations: list[str]) -> None:
    if not previous_path.exists():
        click.echo(
            f"warn: previous database {previous_path} not found -- "
            "skipping row-count comparison (first run?)"
        )
        return
    previous = sqlite3.connect(f"file:{previous_path}?mode=ro", uri=True)
    try:
        for table in KNOWN_TABLES:
            if not table_exists(previous, table):
                continue
            previous_count = row_count(previous, table)
            if not table_exists(conn, table):
                violations.append(
                    f"{table}: present in previous DB ({previous_count} rows) "
                    "but missing from the new build"
                )
                continue
            new_count = row_count(conn, table)
            if table not in NEVER_DECREASE_TABLES:
                click.echo(
                    f"note: {table} row count {previous_count} -> {new_count} "
                    "(exempt from never-decrease: wholesale per-work fragment "
                    "replacement may legitimately shrink it; the per-act "
                    "real-row gate below is the protection)"
                )
                continue
            if new_count < previous_count:
                violations.append(
                    f"{table}: row count decreased {previous_count} -> {new_count} "
                    "(append-mostly invariant: rows are flagged, never deleted)"
                )
            else:
                click.echo(f"ok: {table} row count {previous_count} -> {new_count}")
    finally:
        previous.close()


def check_content_has_fragments(conn: sqlite3.Connection, violations: list[str]) -> None:
    """Every extracted act must have REAL fragment rows.

    The gate counts actual ``acts_fragments`` rows (``parent_id = acts.id``):
    the denormalized ``acts.fragment_count`` is written by the same code path
    that could fail to produce fragments, so trusting it would let a build
    where fragment insertion silently broke sail through. The denormalized
    column is still checked as a cheap consistency signal.
    """
    if not table_exists(conn, "acts"):
        click.echo("warn: acts table does not exist yet -- skipping has_content check")
        return
    columns = table_columns(conn, "acts")
    if not {"has_content", "fragment_count"} <= columns:
        click.echo(
            "warn: acts table lacks has_content/fragment_count columns -- "
            "skipping has_content check (early phase?)"
        )
        return
    label = "shorthand" if "shorthand" in columns else "id" if "id" in columns else "rowid"

    # Denormalized consistency signal (kept from the original check).
    rows = conn.execute(
        f"SELECT {label} FROM acts "
        "WHERE has_content = 1 AND (fragment_count IS NULL OR fragment_count <= 0)"
    ).fetchall()
    if rows:
        sample = ", ".join(str(row[0]) for row in rows[:10])
        violations.append(
            f"acts: {len(rows)} row(s) with has_content=1 but fragment_count<=0 " f"(e.g. {sample})"
        )

    # The real gate: actual acts_fragments rows, not the denormalized count.
    if "id" not in columns:
        click.echo("warn: acts table lacks an id column -- skipping real-fragment-row gate")
        return
    if not table_exists(conn, "acts_fragments"):
        extracted = conn.execute("SELECT COUNT(*) FROM acts WHERE has_content = 1").fetchone()[0]
        if extracted:
            violations.append(
                f"acts: {extracted} row(s) with has_content=1 but the acts_fragments "
                "table does not exist"
            )
        else:
            click.echo("warn: acts_fragments table does not exist yet (no extracted acts)")
        return
    orphans = conn.execute(
        f"SELECT a.{label} FROM acts a "
        "WHERE a.has_content = 1 AND NOT EXISTS "
        "(SELECT 1 FROM acts_fragments f WHERE f.parent_id = a.id)"
    ).fetchall()
    if orphans:
        sample = ", ".join(str(row[0]) for row in orphans[:10])
        violations.append(
            f"acts: {len(orphans)} row(s) with has_content=1 but ZERO actual "
            f"acts_fragments rows (e.g. {sample})"
        )
    if not rows and not orphans:
        click.echo("ok: every acts row with has_content=1 has real fragment rows")


def check_unique_indexes(conn: sqlite3.Connection, violations: list[str]) -> None:
    for table, required_sets in REQUIRED_UNIQUE_INDEXES.items():
        if not table_exists(conn, table):
            click.echo(f"warn: table {table} does not exist yet -- skipping index tripwire")
            continue
        present = unique_index_column_sets(conn, table)
        for required in required_sets:
            pretty = ", ".join(sorted(required))
            if required in present:
                click.echo(f"ok: {table} has unique index on ({pretty})")
            else:
                violations.append(f"{table}: missing required unique index on ({pretty})")


@click.command()
@click.option(
    "--db",
    "db_path",
    default="statutes.db",
    show_default=True,
    type=click.Path(path_type=Path),
    help="Freshly built database to validate.",
)
@click.option(
    "--previous",
    "previous_path",
    default="statutes.previous.db",
    show_default=True,
    type=click.Path(path_type=Path),
    help="Snapshot of the previously deployed database (optional).",
)
def main(db_path: Path, previous_path: Path) -> None:
    """Validate the freshly built statutes database before deployment."""
    if not db_path.exists():
        click.echo(f"FAIL: built database {db_path} does not exist")
        sys.exit(1)

    violations: list[str] = []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        check_row_counts(conn, previous_path, violations)
        check_content_has_fragments(conn, violations)
        check_unique_indexes(conn, violations)
    finally:
        conn.close()

    if violations:
        click.echo("")
        click.echo(f"SANITY CHECKS FAILED -- {len(violations)} violation(s):")
        for violation in violations:
            click.echo(f"  - {violation}")
        click.echo("Refusing to deploy a database that breaks build invariants.")
        sys.exit(1)
    click.echo("All sanity checks passed.")


if __name__ == "__main__":
    main()
