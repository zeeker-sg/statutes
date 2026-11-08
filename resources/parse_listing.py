"""Parser for the SSO Acts browse listing (discovery phase, PRD Phase 1).

Source:   https://sso.agc.gov.sg/Browse/Act/Current/All?PageSize=500&SortBy=Title&SortOrder=ASC
Fixtures: tests/fixtures/listing_p1.html (500 rows) and listing_p2.html
          (23 rows), saved from the live site on 2026-06-11.

Markup anatomy (verified against the fixtures)
----------------------------------------------
The listing is a single ``<table class="table browse-list">`` with one
``<tbody> <tr>`` per Act. Each row carries the same links twice — once in a
mobile-only ("visible-xs") Actions dropdown and once as desktop icon cells —
so every selector below matches the *first* occurrence within the row:

- Title link: the first ``<a href="/Act/{SHORTHAND}">`` in the row. The
  trailing path segment is the work-level shorthand (e.g. ``IA1965``).
- PDF link: ``/Act/{SHORTHAND}?ViewType=Pdf&_={YYYYMMDDHHMMSS}``. The ``_``
  cache-buster is the listing's change-detection stamp; it is captured into
  ``listing_stamp`` and STRIPPED from the stored ``pdf_url`` so the URL stays
  stable across runs.
- RSS link: ``/Act/{SHORTHAND}?ViewType=Rss`` — the per-Act amendments feed.
- Subsidiary Legislation link: ``/Act/{SHORTHAND}?ViewType=Sl`` — present
  only on rows whose Act has SL (→ ``has_sl``).

Pagination (for the crawler)
----------------------------
Page 1's pager exposes page 2 as a *path-segment* page index, zero-based:
``<a aria-label="Next Page" href="/Browse/Act/Current/All/1?PageSize=500&...">``
(i.e. ``/All`` is page 1, ``/All/1`` is page 2). On the last page the
"Next Page" / "Last Page" anchors are absent (only First/Previous remain),
so the crawler follows ``next_page_url()`` until it returns None.

All URLs returned are absolute against ``sso_client.BASE_URL``. ``subtype``
is always None at listing time — the listing template carries no subtype
signal; it is backfilled later from the SubType-filtered listings (PRD,
"Site intelligence").
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

# Zeeker loads resource files via importlib.util.spec_from_file_location,
# which bypasses package imports — ``from resources import ...`` fails at
# build time. Make sibling modules importable by adding this file's
# directory to sys.path before importing them.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sso_client  # noqa: E402

BASE_URL = sso_client.BASE_URL

# Shorthands end in the year of enactment (e.g. IA1965, ACRAA2022); the
# title fallback covers any future shorthand format drift.
_SHORTHAND_YEAR_RE = re.compile(r"(\d{4})$")
_TITLE_YEAR_RE = re.compile(r"\bAct\s+(\d{4})\b")


def _absolute(href: str) -> str:
    return urljoin(BASE_URL, href)


def _strip_stamp(pdf_href: str) -> tuple[str, Optional[str]]:
    """Split a listing PDF href into (absolute URL without ``_``, stamp).

    The ``_`` query param is the cache-buster / last-modified stamp
    (YYYYMMDDHHMMSS). It changes whenever AGC re-consolidates the Act, so it
    is stored separately as ``listing_stamp`` while the persisted ``pdf_url``
    keeps only the stable params (``ViewType=Pdf``).
    """
    parts = urlsplit(_absolute(pdf_href))
    stamp: Optional[str] = None
    kept = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "_":
            stamp = value
        else:
            kept.append((key, value))
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))
    return url, stamp


def _parse_year(shorthand: str, title: str) -> Optional[int]:
    match = _SHORTHAND_YEAR_RE.search(shorthand)
    if match is None:
        match = _TITLE_YEAR_RE.search(title)
    return int(match.group(1)) if match else None


def _first_link(row: Tag, marker: str) -> Optional[Tag]:
    """First anchor in the row whose href contains ``marker``."""
    return row.find("a", href=lambda h: bool(h) and marker in h)


def _parse_row(row: Tag) -> Optional[Dict[str, Any]]:
    title_link = row.find("a", href=lambda h: bool(h) and h.startswith("/Act/") and "?" not in h)
    if title_link is None:
        return None
    source_href = title_link["href"]
    shorthand = urlsplit(source_href).path.rstrip("/").rsplit("/", 1)[-1]
    title = " ".join(title_link.get_text().split())

    pdf_url: Optional[str] = None
    listing_stamp: Optional[str] = None
    pdf_link = _first_link(row, "ViewType=Pdf")
    if pdf_link is not None:
        pdf_url, listing_stamp = _strip_stamp(pdf_link["href"])

    rss_link = _first_link(row, "ViewType=Rss")
    # Every fixture row carries the RSS link; the feed URL is deterministic
    # from the shorthand, so reconstruct it if the markup ever drops it.
    rss_url = (
        _absolute(rss_link["href"])
        if rss_link is not None
        else f"{BASE_URL}/Act/{shorthand}?ViewType=Rss"
    )

    return {
        "shorthand": shorthand,
        "title": title,
        "year": _parse_year(shorthand, title),
        "source_url": _absolute(source_href),
        "pdf_url": pdf_url,
        "listing_stamp": listing_stamp,
        "rss_url": rss_url,
        "has_sl": _first_link(row, "ViewType=Sl") is not None,
        "subtype": None,
    }


def parse_listing_page(html: str) -> List[Dict[str, Any]]:
    """Parse one Acts browse listing page into one dict per Act row.

    Returns dicts with the listing-derived ``acts`` catalog fields:
    ``{shorthand, title, year, source_url, pdf_url, listing_stamp, rss_url,
    has_sl, subtype}``. All URLs are absolute; ``pdf_url`` has the ``_``
    cache-buster stripped (captured into ``listing_stamp`` instead);
    ``subtype`` is always None (see module docstring).
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="browse-list")
    if table is None:
        return []
    rows: List[Dict[str, Any]] = []
    for tr in table.select("tbody > tr"):
        parsed = _parse_row(tr)
        if parsed is not None:
            rows.append(parsed)
    return rows


def next_page_url(html: str) -> Optional[str]:
    """Absolute URL of the next listing page, or None on the last page.

    Page 1's pager links page 2 as ``/Browse/Act/Current/All/1?PageSize=...``
    — a zero-based page index appended as a path segment. The "Next Page"
    anchor (matched by aria-label) is simply absent on the last page.
    """
    soup = BeautifulSoup(html, "lxml")
    link = soup.find("a", attrs={"aria-label": "Next Page"}, href=True)
    if link is None:
        return None
    return _absolute(link["href"])
