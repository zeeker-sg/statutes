"""Tests for resources/parse_detail.py against the committed detail fixtures.

Fixtures were saved from the live site on 2026-06-11:
- act_IA1965_raw.html  — long Act (Interpretation Act 1965), 8 lazy-load
  placeholders, 35 distinct timeline validity dates (the 2015-01-01 version
  is rendered as three <li> rows, one per amending Act).
- act_AA1928_raw.html  — short Act (Apportionment Act 1928), 2 lazy-load
  placeholders (one body series + LEGISLATION_ABBREVIATIONS), 3 timeline rows.
- lazyload_urls.txt    — the 8 GetLazyLoadContent XHR URLs captured from the
  live IA1965 page; build_lazyload_url must reconstruct them exactly.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import parse_detail
import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"

BASE = "https://sso.agc.gov.sg"

# Real counts in the committed fixtures.
IA1965_SERIES_COUNT = 8  # dms placeholders == captured XHR URLs
IA1965_VERSION_COUNT = 35  # 37 timeline <li> rows, 2015-01-01 merged from 3
IA1965_TOC_ENTRIES = 69  # matches the 69 prov1Hdr cells in the rendered fixture
AA1928_SERIES_COUNT = 2
AA1928_VERSION_COUNT = 3
AA1928_TOC_ENTRIES = 7


@pytest.fixture(scope="module")
def ia1965():
    return parse_detail.parse_detail_page((FIXTURES / "act_IA1965_raw.html").read_text())


@pytest.fixture(scope="module")
def aa1928():
    return parse_detail.parse_detail_page((FIXTURES / "act_AA1928_raw.html").read_text())


@pytest.fixture(scope="module")
def captured_urls():
    lines = (FIXTURES / "lazyload_urls.txt").read_text().strip().splitlines()
    return [line.strip() for line in lines if line.strip()]


def _url_parts(url: str):
    parts = urlsplit(url)
    return (
        parts.scheme,
        parts.netloc,
        parts.path,
        sorted(parse_qsl(parts.query, keep_blank_values=True)),
    )


# =============================================================================
# Top-level fields
# =============================================================================
def test_ia1965_identity(ia1965):
    assert ia1965["shorthand"] == "IA1965"
    assert ia1965["long_title"].startswith("An Act to define certain terms and expressions")
    assert ia1965["current_as_at"] == "2026-06-11"
    assert ia1965["toc_entries"] == IA1965_TOC_ENTRIES


def test_aa1928_identity(aa1928):
    assert aa1928["shorthand"] == "AA1928"
    assert aa1928["long_title"] == (
        "An Act for the better apportionment of rents and other periodical payments."
    )
    assert aa1928["current_as_at"] == "2026-06-11"
    assert aa1928["toc_entries"] == AA1928_TOC_ENTRIES


# =============================================================================
# Version timeline
# =============================================================================
def test_ia1965_timeline_row_count(ia1965):
    assert len(ia1965["versions"]) == IA1965_VERSION_COUNT


def test_ia1965_first_version_is_1985_reved(ia1965):
    first = ia1965["versions"][0]
    assert first["valid_date"] == "1987-03-30"
    assert first["doc_date"] == "1991-01-25"
    assert first["amended_by"] == "1985 RevEd"
    assert first["historical_url"] == (
        f"{BASE}/Act/IA1965/Historical/19870330?DocDate=19910125&ValidDate=19870330"
    )
    assert first["pdf_url"] is None  # oldest editions carry no PDF link
    assert first["is_current"] is False


def test_ia1965_last_version_is_current(ia1965):
    last = ia1965["versions"][-1]
    assert last["valid_date"] == "2025-12-05"
    assert last["is_current"] is True
    # The current row's anchor is /Act/IA1965?ValidDate=... (not /Historical/)
    # and carries no DocDate param.
    assert last["historical_url"] is None
    assert last["doc_date"] is None
    assert last["amended_by"] == "Act 19 of 2025"
    # PDF link present, absolute, volatile "_" cache-buster stripped.
    assert last["pdf_url"] == f"{BASE}/Act/IA1965?ValidDate=20251205&ViewType=Pdf"


def test_ia1965_exactly_one_current_row(ia1965):
    assert sum(1 for v in ia1965["versions"] if v["is_current"]) == 1


def test_ia1965_same_valid_date_rows_merge(ia1965):
    """2015-01-01 is rendered as three <li> rows (one per amending Act)."""
    merged = [v for v in ia1965["versions"] if v["valid_date"] == "2015-01-01"]
    assert len(merged) == 1
    assert merged[0]["amended_by"] == "Act 27 of 2014; Act 41 of 2014; Act 42 of 2014"


def test_ia1965_version_urls_absolute_and_stamp_free(ia1965):
    for version in ia1965["versions"]:
        if version["historical_url"] is not None:
            assert version["historical_url"].startswith(f"{BASE}/Act/IA1965/Historical/")
        if version["pdf_url"] is not None:
            assert version["pdf_url"].startswith(BASE)
            assert "_=" not in version["pdf_url"]


def test_ia1965_versions_in_document_order_oldest_first(ia1965):
    dates = [v["valid_date"] for v in ia1965["versions"]]
    assert dates == sorted(dates)


def test_aa1928_timeline(aa1928):
    versions = aa1928["versions"]
    assert len(versions) == AA1928_VERSION_COUNT
    assert [v["valid_date"] for v in versions] == ["1987-03-30", "1998-12-15", "2021-12-31"]
    assert [v["amended_by"] for v in versions] == ["1985 RevEd", "1998 RevEd", "2020 RevEd"]
    assert [v["is_current"] for v in versions] == [False, False, True]


# =============================================================================
# Lazy-load plan
# =============================================================================
def test_ia1965_lazyload_series_match_placeholder_count(ia1965):
    lazyload = ia1965["lazyload"]
    assert lazyload is not None
    assert len(lazyload["series"]) == IA1965_SERIES_COUNT
    assert lazyload["content_url"] == f"{BASE}/Details/GetLazyLoadContent"
    assert lazyload["toc_sys_id"] == "605483f8-ddc4-4cfe-b17b-d530b729625e"
    # Every placeholder resolved against the page's fragments map.
    assert all(entry["frag_sys_id"] and entry["stamp"] for entry in lazyload["series"])


def test_ia1965_lazyload_includes_special_abbreviations_series(ia1965):
    """LEGISLATION_ABBREVIATIONS maps to itself in the fragments map."""
    special = ia1965["lazyload"]["series"][-1]
    assert special["series_id"] == "LEGISLATION_ABBREVIATIONS"
    assert special["frag_sys_id"] == "LEGISLATION_ABBREVIATIONS"
    assert special["stamp"] == "20200701000000"


def test_aa1928_lazyload_has_two_series(aa1928):
    """Short Acts still lazy-load: one body series + the abbreviations key."""
    lazyload = aa1928["lazyload"]
    assert lazyload is not None
    assert len(lazyload["series"]) == AA1928_SERIES_COUNT
    assert lazyload["series"][0]["series_id"] == "d008da17-231f-4aa9-b4fb-1b02fedfb8ed"
    assert lazyload["series"][1]["series_id"] == "LEGISLATION_ABBREVIATIONS"
    assert lazyload["toc_sys_id"] == "7f10a6b1-27c8-41dd-aa7c-67ed7105c802"


def test_lazyload_none_when_no_placeholders():
    assert parse_detail.parse_detail_page("<html><body><p>hi</p></body></html>")["lazyload"] is None


# =============================================================================
# URL reconstruction against the captured XHRs
# =============================================================================
def test_build_lazyload_url_reconstructs_first_captured_url(ia1965, captured_urls):
    lazyload = ia1965["lazyload"]
    built = parse_detail.build_lazyload_url(lazyload, lazyload["series"][0])
    # Param order may differ from the capture; compare parsed components.
    assert _url_parts(built) == _url_parts(captured_urls[0])


def test_build_lazyload_url_reconstructs_every_captured_url(ia1965, captured_urls):
    """All 8 captured XHRs (incl. legislative history + LEGISLATION_ABBREVIATIONS)
    rebuild from the raw page data, in placeholder document order."""
    lazyload = ia1965["lazyload"]
    assert len(captured_urls) == IA1965_SERIES_COUNT
    built = [parse_detail.build_lazyload_url(lazyload, entry) for entry in lazyload["series"]]
    assert [_url_parts(b) for b in built] == [_url_parts(c) for c in captured_urls]


def test_build_lazyload_url_exact_string_match(ia1965, captured_urls):
    """The serialization order (lazyLoadFilter keys, then FragSysId, then _)
    reproduces the captured URL byte-for-byte, not just semantically."""
    lazyload = ia1965["lazyload"]
    assert parse_detail.build_lazyload_url(lazyload, lazyload["series"][0]) == captured_urls[0]


def test_build_lazyload_url_rejects_unresolved_series(ia1965):
    with pytest.raises(ValueError):
        parse_detail.build_lazyload_url(
            ia1965["lazyload"], {"series_id": "not-in-map", "frag_sys_id": None, "stamp": None}
        )
