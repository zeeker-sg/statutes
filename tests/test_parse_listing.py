"""Tests for resources/parse_listing.py against the committed listing fixtures.

Fixtures were saved from the live site on 2026-06-11:
- listing_p1.html — Browse/Act/Current/All?PageSize=500&SortBy=Title&SortOrder=ASC, page 1
- listing_p2.html — same listing, page 2 (path-segment page index /All/1)
"""

from __future__ import annotations

from pathlib import Path

import parse_listing
import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"

BASE = "https://sso.agc.gov.sg"

# Real row counts in the committed fixtures (500-row page 1 + 23-row page 2
# = 523 current Acts as captured; the PRD's "525" was the live-site estimate).
P1_ROW_COUNT = 500
P2_ROW_COUNT = 23


@pytest.fixture(scope="module")
def p1_rows():
    return parse_listing.parse_listing_page((FIXTURES / "listing_p1.html").read_text())


@pytest.fixture(scope="module")
def p2_rows():
    return parse_listing.parse_listing_page((FIXTURES / "listing_p2.html").read_text())


@pytest.fixture(scope="module")
def all_rows(p1_rows, p2_rows):
    return p1_rows + p2_rows


# =============================================================================
# Row counts
# =============================================================================
def test_page1_yields_exact_row_count(p1_rows):
    assert len(p1_rows) == P1_ROW_COUNT


def test_page2_yields_exact_row_count(p2_rows):
    assert len(p2_rows) == P2_ROW_COUNT


def test_total_catalog_size(all_rows):
    assert len(all_rows) == P1_ROW_COUNT + P2_ROW_COUNT == 523


# =============================================================================
# Spot check: IA1965 (Interpretation Act 1965)
# =============================================================================
@pytest.fixture(scope="module")
def ia1965(p1_rows):
    matches = [r for r in p1_rows if r["shorthand"] == "IA1965"]
    assert len(matches) == 1
    return matches[0]


def test_ia1965_title_and_year(ia1965):
    assert ia1965["title"] == "Interpretation Act 1965"
    assert ia1965["year"] == 1965


def test_ia1965_source_url_absolute(ia1965):
    assert ia1965["source_url"] == f"{BASE}/Act/IA1965"


def test_ia1965_listing_stamp_present_and_stripped_from_pdf_url(ia1965):
    # Stamp captured from the listing's PDF cache-buster (the change-detection
    # key) and removed from the stored pdf_url.
    assert ia1965["listing_stamp"] == "20260602205225"
    assert ia1965["pdf_url"] == f"{BASE}/Act/IA1965?ViewType=Pdf"
    assert "_=" not in ia1965["pdf_url"]


def test_ia1965_rss_url_absolute(ia1965):
    assert ia1965["rss_url"] == f"{BASE}/Act/IA1965?ViewType=Rss"


def test_ia1965_has_sl_and_null_subtype(ia1965):
    assert ia1965["has_sl"] is True
    assert ia1965["subtype"] is None


# =============================================================================
# Whole-catalog invariants
# =============================================================================
def test_every_row_has_nonempty_identity_fields(all_rows):
    for row in all_rows:
        assert row["shorthand"], row
        assert row["title"], row
        assert row["source_url"], row


def test_shorthands_unique_across_both_pages(all_rows):
    shorthands = [row["shorthand"] for row in all_rows]
    assert len(shorthands) == len(set(shorthands))


def test_all_urls_absolute_and_pdf_urls_stamp_free(all_rows):
    for row in all_rows:
        assert row["source_url"].startswith(f"{BASE}/Act/")
        assert row["rss_url"].startswith(f"{BASE}/Act/")
        assert row["rss_url"].endswith("ViewType=Rss")
        assert row["pdf_url"] is not None and row["pdf_url"].startswith(f"{BASE}/Act/")
        assert "_=" not in row["pdf_url"]


def test_every_row_has_a_listing_stamp(all_rows):
    # Every fixture row's PDF link carries the "_" cache-buster; the stamp is
    # 14 digits (YYYYMMDDHHMMSS).
    for row in all_rows:
        assert row["listing_stamp"] is not None, row["shorthand"]
        assert len(row["listing_stamp"]) == 14 and row["listing_stamp"].isdigit(), row


def test_every_row_has_a_plausible_year(all_rows):
    for row in all_rows:
        assert isinstance(row["year"], int), row["shorthand"]
        assert 1800 <= row["year"] <= 2026, row


def test_has_sl_varies_across_catalog(all_rows):
    # The listing mixes Acts with and without subsidiary legislation; both
    # boolean values must occur (fixture counts: 360 + 17 True).
    flags = [row["has_sl"] for row in all_rows]
    assert any(flags) and not all(flags)
    assert sum(flags) == 377


def test_year_falls_back_to_title_when_shorthand_has_no_digits():
    html = """
    <table class="table browse-list"><tbody>
      <tr><td><a class="non-ajax" href="/Act/XYZ">Example Act 1999</a></td></tr>
    </tbody></table>
    """
    rows = parse_listing.parse_listing_page(html)
    assert len(rows) == 1
    assert rows[0]["year"] == 1999
    assert rows[0]["listing_stamp"] is None
    assert rows[0]["pdf_url"] is None
    assert rows[0]["has_sl"] is False


# =============================================================================
# Pagination helper (the crawler follows this to page 2)
# =============================================================================
def test_page1_pager_exposes_page2_url():
    url = parse_listing.next_page_url((FIXTURES / "listing_p1.html").read_text())
    # Path-segment page index, zero-based: /All is page 1, /All/1 is page 2.
    assert url == f"{BASE}/Browse/Act/Current/All/1?PageSize=500&SortBy=Title&SortOrder=ASC"


def test_page2_is_the_last_page():
    assert parse_listing.next_page_url((FIXTURES / "listing_p2.html").read_text()) is None
