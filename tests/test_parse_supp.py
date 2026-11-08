"""Tests for resources/parse_supp.py against committed supplement fixtures.

Fixtures (saved from the live site 2026-06-11):

- ``acts_supp_2026.html`` — /Browse/Acts-Supp/Published/2026 page 1
  (PageSize=20, SortBy=Title ASC): 15 rows.
- ``sl_supp_2026.html`` — /Browse/SL-Supp/Published/2026 page 1
  (PageSize=20, SortBy=Title ASC): 20 rows of "370 results in 19 pages".

Both fixture pages are sorted by Title ASC (not Number DESC), so order
tests pin the exact gazette-number sequence as it appears in the document
rather than asserting monotonicity.
"""

from __future__ import annotations

from pathlib import Path

import parse_supp
import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="module")
def acts_rows():
    html = (FIXTURES / "acts_supp_2026.html").read_text(encoding="utf-8")
    return parse_supp.parse_supp_page(html, "acts_supp")


@pytest.fixture(scope="module")
def sl_rows():
    html = (FIXTURES / "sl_supp_2026.html").read_text(encoding="utf-8")
    return parse_supp.parse_supp_page(html, "sl_supp")


# =============================================================================
# Row counts and document order
# =============================================================================
def test_acts_supp_row_count(acts_rows):
    assert len(acts_rows) == 15


def test_sl_supp_row_count(sl_rows):
    assert len(sl_rows) == 20


def test_acts_supp_document_order_pinned(acts_rows):
    # Page is Title ASC; pin the exact number sequence in document order.
    assert [r["number_seq"] for r in acts_rows] == [
        8, 16, 10, 1, 13, 5, 3, 2, 4, 17, 15, 7, 6, 9, 11,
    ]  # fmt: skip


def test_sl_supp_document_order_pinned(sl_rows):
    assert [r["number_seq"] for r in sl_rows] == [
        202, 201, 200, 234, 355, 358, 278, 81, 121, 218,
        306, 217, 80, 356, 128, 289, 248, 233, 105, 249,
    ]  # fmt: skip


def test_titles_are_ascending_as_page_claims(acts_rows, sl_rows):
    for rows in (acts_rows, sl_rows):
        titles = [r["title"] for r in rows]
        assert titles == sorted(titles)


# =============================================================================
# First-row end-to-end spot checks
# =============================================================================
def test_acts_supp_first_row_end_to_end(acts_rows):
    row = acts_rows[0]
    assert row == {
        "series": "acts_supp",
        "number": "8 of 2026",  # shown as "Act 8 of 2026"; "Act " prefix stripped
        "number_seq": 8,
        "year": 2026,
        "title": "Coastal Protection and Other Amendments Act 2026",
        "published_date": "2026-04-27",
        "source_url": "https://sso.agc.gov.sg/Acts-Supp/8-2026/Published/20260427?DocDate=20260427",
        "pdf_url": (
            "https://sso.agc.gov.sg/Acts-Supp/8-2026/Published/20260427"
            "?DocDate=20260427&ViewType=Pdf"
        ),
        # Omnibus "… and Other Amendments Act" has no clean marker -> other
        # (conservative: principal extraction here would be a guess).
        "event_kind": "other",
        "principal_title": None,
    }


def test_sl_supp_first_row_end_to_end(sl_rows):
    row = sl_rows[0]
    assert row == {
        "series": "sl_supp",
        "number": "S 202/2026",
        "number_seq": 202,
        "year": 2026,
        "title": (
            "Accountants (Prescribed Standards and Code of Professional Conduct "
            "and Ethics) (Amendment) Order 2026"
        ),
        "published_date": "2026-04-06",
        "source_url": "https://sso.agc.gov.sg/SL-Supp/S202-2026/Published/20260406?DocDate=20260406",
        "pdf_url": (
            "https://sso.agc.gov.sg/SL-Supp/S202-2026/Published/20260406"
            "?DocDate=20260406&ViewType=Pdf"
        ),
        "event_kind": "amendment",
        "principal_title": (
            "Accountants (Prescribed Standards and Code of Professional Conduct "
            "and Ethics) Order"
        ),
    }


# =============================================================================
# Field invariants across every row
# =============================================================================
def test_all_rows_share_contract_invariants(acts_rows, sl_rows):
    for series, rows in (("acts_supp", acts_rows), ("sl_supp", sl_rows)):
        for row in rows:
            assert row["series"] == series
            assert row["year"] == 2026
            assert isinstance(row["number_seq"], int) and row["number_seq"] > 0
            assert row["source_url"].startswith("https://sso.agc.gov.sg/")
            assert row["published_date"].startswith("2026-")
            assert len(row["published_date"]) == 10
            assert row["pdf_url"] is None or (
                row["pdf_url"].startswith("https://sso.agc.gov.sg/")
                and "_=" not in row["pdf_url"]
                and "ViewType=Pdf" in row["pdf_url"]
            )
            assert row["event_kind"] in (
                "new",
                "amendment",
                "revocation",
                "commencement",
                "corrigenda",
                "other",
            )


def test_number_format_per_series(acts_rows, sl_rows):
    for row in acts_rows:
        assert row["number"] == f"{row['number_seq']} of 2026"
    for row in sl_rows:
        assert row["number"] == f"S {row['number_seq']}/2026"


def test_acts_supp_fixture_classifications(acts_rows):
    by_seq = {r["number_seq"]: r for r in acts_rows}
    # Clean "(Amendment)" markers -> amendment with normalized principal.
    assert by_seq[10]["event_kind"] == "amendment"
    assert by_seq[10]["principal_title"] == "Energy Conservation Act"
    assert by_seq[2]["principal_title"] == "Public Sector (Governance) Act"
    # "(Amendment) and Other Matters" -> omnibus tail removed from principal.
    assert by_seq[9]["event_kind"] == "amendment"
    assert by_seq[9]["principal_title"] == "Tobacco (Control of Advertisements and Sale) Act"
    # Plain new Acts.
    assert by_seq[1]["event_kind"] == "new"  # Health Information Act 2026
    assert by_seq[6]["event_kind"] == "new"  # Supply Act 2026
    # Omnibus amendment titles without a clean marker -> other, no principal.
    assert by_seq[15]["event_kind"] == "other"  # Statutes (Miscellaneous Amendments)
    assert by_seq[15]["principal_title"] is None


def test_parse_supp_page_rejects_unknown_series():
    with pytest.raises(ValueError):
        parse_supp.parse_supp_page("<html></html>", "bills_supp")


# =============================================================================
# classify_event — table-driven over all six kinds
# =============================================================================
@pytest.mark.parametrize(
    ("title", "kind", "principal"),
    [
        # -- amendment: paren marker --------------------------------------
        (
            "Road Traffic (Amendment) Act 2026",
            "amendment",
            "Road Traffic Act",
        ),
        (
            "Active Mobility (Amendment) Regulations 2026",
            "amendment",
            "Active Mobility Regulations",
        ),
        (
            "Active Mobility (Certificate of Medical Need) (Amendment) Regulations 2026",
            "amendment",
            "Active Mobility (Certificate of Medical Need) Regulations",
        ),
        # -- amendment: numbered marker -----------------------------------
        (
            "Income Tax (Exemption of Foreign Income) (Amendment No. 2) Regulations 2026",
            "amendment",
            "Income Tax (Exemption of Foreign Income) Regulations",
        ),
        # -- amendment: bare "Amendment Act" form --------------------------
        (
            "Singapore Armed Forces Amendment Act 2026",
            "amendment",
            "Singapore Armed Forces Act",
        ),
        # -- amendment: omnibus tail stripped from principal ----------------
        (
            "Tobacco (Control of Advertisements and Sale) (Amendment) and Other Matters Act 2026",
            "amendment",
            "Tobacco (Control of Advertisements and Sale) Act",
        ),
        # -- revocation ------------------------------------------------------
        (
            "Banking (Exemption) (Revocation) Notification 2026",
            "revocation",
            "Banking (Exemption) Notification",
        ),
        # -- commencement (wins over the embedded "(Amendment)" marker) -----
        (
            "Energy Conservation (Amendment) Act 2026 (Commencement) Notification 2026",
            "commencement",
            None,
        ),
        (
            "Health Information Act 2026 (Commencement No. 2) Notification 2026",
            "commencement",
            None,
        ),
        # -- corrigenda (wins over the embedded "(Amendment)" marker) -------
        (
            "Road Traffic (Amendment) Rules 2026 (Corrigenda)",
            "corrigenda",
            None,
        ),
        ("Corrigendum to the Supply Act 2026", "corrigenda", None),
        # -- new -------------------------------------------------------------
        ("Health Information Act 2026", "new", None),
        (
            "Active Mobility (Certificate of Medical Need — Exemption) Order 2026",
            "new",
            None,
        ),
        # -- other: amendment-flavoured but no clean marker (conservative) --
        ("Statutes (Miscellaneous Amendments) Act 2026", "other", None),
        ("Coastal Protection and Other Amendments Act 2026", "other", None),
        (
            "Misuse of Drugs Act (Amendment of First Schedule) Order 2026",
            "other",
            None,
        ),
    ],
)
def test_classify_event_table(title, kind, principal):
    assert parse_supp.classify_event(title) == (kind, principal)
