"""Focused tests for scripts/sanity_checks.py invariants.

The end-to-end invariants run inside tests/test_integration_offline.py
against a real built database; these tests pin the two checks whose edge
semantics the pipeline cannot exercise: the acts_fragments exemption from
the never-decrease invariant (wholesale per-work fragment replacement may
legitimately shrink the table) and the real-row gate behind
``has_content = 1``.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_sanity() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "sanity_checks", PROJECT_ROOT / "scripts" / "sanity_checks.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_db(path: Path, acts_rows: int, fragment_rows: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE acts (id TEXT, shorthand TEXT, has_content INT, fragment_count INT)")
    conn.execute("CREATE TABLE acts_fragments (id TEXT, parent_id TEXT, provision_id TEXT)")
    for i in range(acts_rows):
        conn.execute("INSERT INTO acts VALUES (?, ?, 0, 0)", (f"id{i}", f"SH{i}"))
    for i in range(fragment_rows):
        conn.execute("INSERT INTO acts_fragments VALUES (?, ?, ?)", (f"f{i}", "id0", f"p{i}"))
    conn.commit()
    conn.close()


def test_fragment_shrinkage_is_exempt_but_acts_shrinkage_violates(tmp_path):
    """Re-consolidation legitimately shrinks acts_fragments (wholesale
    delete-then-reinsert per work, PRD); catalog tables stay append-mostly."""
    sanity = _load_sanity()
    previous, new = tmp_path / "previous.db", tmp_path / "new.db"
    _make_db(previous, acts_rows=5, fragment_rows=10)
    _make_db(new, acts_rows=5, fragment_rows=7)  # fragments shrank: fine

    conn = sqlite3.connect(new)
    try:
        violations: list[str] = []
        sanity.check_row_counts(conn, previous, violations)
        assert violations == []
    finally:
        conn.close()

    shrunk_acts = tmp_path / "shrunk.db"
    _make_db(shrunk_acts, acts_rows=4, fragment_rows=10)  # catalog shrank: never OK
    conn = sqlite3.connect(shrunk_acts)
    try:
        violations = []
        sanity.check_row_counts(conn, previous, violations)
        assert len(violations) == 1
        assert violations[0].startswith("acts:")
    finally:
        conn.close()


def test_has_content_requires_real_fragment_rows_not_just_the_counter(tmp_path):
    """The denormalized fragment_count must not satisfy the gate: an act
    claiming content needs actual acts_fragments rows."""
    sanity = _load_sanity()
    db_path = tmp_path / "build.db"
    _make_db(db_path, acts_rows=1, fragment_rows=0)
    conn = sqlite3.connect(db_path)
    try:
        # has_content=1 with a confident-looking fragment_count but ZERO rows.
        conn.execute("UPDATE acts SET has_content = 1, fragment_count = 8")
        violations: list[str] = []
        sanity.check_content_has_fragments(conn, violations)
        assert len(violations) == 1
        assert "ZERO actual" in violations[0]

        # One real row satisfies the gate.
        conn.execute("INSERT INTO acts_fragments VALUES ('f1', 'id0', 'p1')")
        violations = []
        sanity.check_content_has_fragments(conn, violations)
        assert violations == []
    finally:
        conn.close()
