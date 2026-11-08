"""Parser for SSO Act detail pages (PRD Phase 2, step 1 — no IO, no HTTP).

Source:   https://sso.agc.gov.sg/Act/{SHORTHAND}
Fixtures: tests/fixtures/act_IA1965_raw.html (long Act, 8 lazy-load
          placeholders) and act_AA1928_raw.html (short Act, 2 placeholders),
          saved from the live site on 2026-06-11.

Input is the *initial* server-rendered HTML (before any lazy-load XHR);
output is the dict consumed by ``acts.py`` (extraction) and
``act_versions.py`` (timeline rows).

Markup anatomy (verified against the fixtures)
----------------------------------------------
- **Status banner**: ``<div class="status-value">Current version as at
  11 Jun 2026</div>`` → ``current_as_at`` (ISO date).
- **Front matter**: ``<td class="longTitle">`` inside ``div.front`` →
  ``long_title``.
- **TOC**: ``<nav id="toc">`` with one ``a.nav-link[data-part]`` per
  provision entry (Part/Schedule headings are ``a.nav-link`` *without*
  ``data-part``) → ``toc_entries`` counts the provision-level links.
- **Version timeline**: ``<div id="docTimeline"> <ul> <li data-id=...>`` —
  one ``li`` per point-in-time expression, oldest first, the current row
  marked ``class="selected"``. Each row carries a date anchor
  (``/Act/{SH}/Historical/{VALID}?DocDate=...&ValidDate=...`` for
  historical rows; ``/Act/{SH}?ValidDate=...`` for the current row), an
  optional ``a.file-download`` PDF link, and a ``div.group_status`` label —
  either an amending-Act anchor ("Act 19 of 2025") or a revised-edition
  span ("1985 RevEd"). Quirks encoded here:

  * ``valid_date``/``doc_date`` come from the URL params (the popover's
    ``data-date`` is the *publish* date, which differs from ``DocDate``);
    the current row has no ``DocDate`` param → ``doc_date`` is None.
  * A validity date reached by several amending Acts is rendered as
    *multiple* ``li`` rows with the same ``data-id`` (IA1965's 2015-01-01
    appears three times). Rows are merged on ``valid_date`` — the PRD pins
    one expression per work per validity date — with the labels joined
    ("Act 27 of 2014; Act 41 of 2014; Act 42 of 2014").
  * Version PDF hrefs carry a volatile ``_`` cache-buster that changes on
    every render; it is STRIPPED (same rule as the listing parser) so
    ``act_versions`` rows stay stable across runs.
  * ``historical_url`` is the absolute date-anchor href for historical
    rows and None for the current row (whose URL is just the Act's own
    ``source_url`` plus ``ValidDate``).

Lazy-load protocol (see tests/fixtures/lazyload_curl.txt)
---------------------------------------------------------
Long Acts stream body series in via XHR. Everything needed to replay the
page's own requests is server-rendered:

- Placeholders: ``<div class="dms" data-field="seriesId"
  data-term="{SeriesId}">`` in document order.
- Globals: the page-level ``<div class="global-vars" data-json='{...}'>``
  (there are two ``global-vars`` divs; the page-level one is the one
  carrying ``lazyLoadFilter``). It holds ``lazyLoadContentUrl``
  ("/Details/GetLazyLoadContent"), ``lazyLoadFilter`` (TocSysId + a tail of
  null search params — serialized in JSON order, nulls as empty strings,
  this is exactly the captured query-string prefix), and a ``fragments``
  map ``SeriesId -> {Item1: FragSysId, Item2: "_" cache-buster stamp}``.
- The request the site's JS issues is then
  ``GET {lazyLoadContentUrl}?{lazyLoadFilter with SeriesId filled}
  &FragSysId={Item1}&_={Item2}`` — reconstructed by
  :func:`build_lazyload_url` and asserted against the captured URLs in
  tests/fixtures/lazyload_urls.txt.
- SPECIAL series: the Abbreviations placeholder uses the sentinel term
  ``LEGISLATION_ABBREVIATIONS``, which the fragments map maps to *itself*
  (FragSysId ``LEGISLATION_ABBREVIATIONS``, fixed stamp ``20200701000000``)
  — so it needs no special-casing, the generic map lookup covers it. The
  legislative-history series is an ordinary GUID entry in the map.

``lazyload`` is None only when the page has no dms placeholders; both
committed fixtures have at least the body series + abbreviations. The
``?ViewType=Print`` route is NOT an alternative: the captured
act_IA1965_print.html is a 43KB shell with zero provisions, so the
lazy-load replay above is the only full-text route (PRD Phase 0, route 1).

All URLs returned are absolute against ``sso_client.BASE_URL``.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
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

# Default endpoint path, used only if the globals blob ever drops
# lazyLoadContentUrl (it is present in both fixtures).
LAZYLOAD_CONTENT_PATH = "/Details/GetLazyLoadContent"

_BANNER_RE = re.compile(r"Current version as at\s+(\d{1,2}\s+\w{3}\s+\d{4})")
_COMPACT_DATE_RE = re.compile(r"^\d{8}$")


def _absolute(href: str) -> str:
    return urljoin(BASE_URL, href)


def _iso_from_compact(value: Optional[str]) -> Optional[str]:
    """``YYYYMMDD`` → ``YYYY-MM-DD`` (None for missing/malformed values)."""
    if not value or not _COMPACT_DATE_RE.match(value):
        return None
    return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"


def _strip_stamp(href: str) -> str:
    """Absolute URL with the volatile ``_`` cache-buster param removed."""
    parts = urlsplit(_absolute(href))
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "_"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def _query_params(href: str) -> Dict[str, str]:
    return dict(parse_qsl(urlsplit(href).query, keep_blank_values=True))


# ---------------------------------------------------------------------------
# Page globals (div.global-vars data-json)
# ---------------------------------------------------------------------------
def _page_globals(soup: BeautifulSoup) -> Dict[str, Any]:
    """The page-level globals blob (the global-vars div with lazyLoadFilter).

    Detail pages carry two ``div.global-vars`` blobs: a site-level one
    (labels, API client ids) and the page-level one with ``lazyLoadFilter``,
    ``fragments``, ``currViewFilter`` etc. Returns ``{}`` if absent so every
    consumer degrades to its markup fallback.
    """
    for div in soup.find_all("div", class_="global-vars"):
        raw = div.get("data-json")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict) and "lazyLoadFilter" in data:
            return data
    return {}


def _parse_shorthand(page_globals: Dict[str, Any]) -> Optional[str]:
    doc_no = (page_globals.get("currViewFilter") or {}).get("DocNo")
    if doc_no:
        return str(doc_no)
    # Fallback: trailing segment of the page's own URL path ("/Act/IA1965").
    path = page_globals.get("currUrlPath") or ""
    segment = path.rstrip("/").rsplit("/", 1)[-1]
    return segment or None


# ---------------------------------------------------------------------------
# Banner, front matter, TOC
# ---------------------------------------------------------------------------
def _parse_current_as_at(soup: BeautifulSoup) -> Optional[str]:
    banner = soup.find(string=_BANNER_RE)
    if banner is None:
        return None
    match = _BANNER_RE.search(str(banner))
    if match is None:  # pragma: no cover — find() matched the same regex
        return None
    try:
        return datetime.strptime(match.group(1), "%d %b %Y").date().isoformat()
    except ValueError:
        return None


def _parse_long_title(soup: BeautifulSoup) -> Optional[str]:
    cell = soup.find("td", class_="longTitle")
    if cell is None:
        return None
    text = " ".join(cell.get_text(" ", strip=True).split())
    return text or None


def _count_toc_entries(soup: BeautifulSoup) -> int:
    """Provision-level TOC links: ``a.nav-link[data-part]`` inside nav#toc.

    Part/Schedule headings are nav-links *without* ``data-part``; counting
    only the data-part links makes ``toc_entries`` the number of provisions
    the body parser is expected to yield (cross-checked: IA1965's 69 TOC
    entries match the 69 ``prov1Hdr`` cells in the rendered fixture).
    """
    toc = soup.find("nav", id="toc")
    if toc is None:
        return 0
    return len(toc.find_all("a", class_="nav-link", attrs={"data-part": True}))


# ---------------------------------------------------------------------------
# Version timeline
# ---------------------------------------------------------------------------
def _timeline_label(row: Tag) -> Optional[str]:
    """The group_status label: amending-Act anchor text or RevEd span text."""
    status = row.find("div", class_="group_status")
    if status is None:
        return None
    anchor = status.find("a")
    if anchor is not None:
        label = " ".join(anchor.get_text(" ", strip=True).split())
        return label or None
    text = " ".join(status.get_text(" ", strip=True).split())
    return text or None


def _parse_timeline_row(row: Tag) -> Optional[Dict[str, Any]]:
    date_link = row.find("a", href=True)
    if date_link is None:
        return None
    href = date_link["href"]
    params = _query_params(href)
    valid_date = _iso_from_compact(params.get("ValidDate"))
    if valid_date is None and "/Historical/" in href:
        # Belt-and-braces: the validity date is also the path segment.
        valid_date = _iso_from_compact(urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1])
    if valid_date is None:
        return None

    pdf_link = row.find("a", class_="file-download", href=True)
    return {
        "valid_date": valid_date,
        "doc_date": _iso_from_compact(params.get("DocDate")),
        "amended_by": _timeline_label(row),
        "historical_url": _absolute(href) if "/Historical/" in href else None,
        "pdf_url": _strip_stamp(pdf_link["href"]) if pdf_link is not None else None,
        "is_current": "selected" in (row.get("class") or []),
    }


def _parse_versions(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    timeline = soup.find("div", id="docTimeline")
    if timeline is None:
        return []
    versions: List[Dict[str, Any]] = []
    by_valid_date: Dict[str, Dict[str, Any]] = {}
    for row in timeline.find_all("li", attrs={"data-id": True}):
        parsed = _parse_timeline_row(row)
        if parsed is None:
            continue
        existing = by_valid_date.get(parsed["valid_date"])
        if existing is None:
            by_valid_date[parsed["valid_date"]] = parsed
            versions.append(parsed)
            continue
        # Same validity date reached by several amending Acts → one merged
        # row (the PRD pins one expression per work per validity date).
        label = parsed["amended_by"]
        if label and label != existing["amended_by"]:
            existing["amended_by"] = (
                f"{existing['amended_by']}; {label}" if existing["amended_by"] else label
            )
        existing["is_current"] = existing["is_current"] or parsed["is_current"]
    return versions


# ---------------------------------------------------------------------------
# Lazy-load plan
# ---------------------------------------------------------------------------
def _parse_lazyload(soup: BeautifulSoup, page_globals: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    placeholders = soup.find_all("div", class_="dms", attrs={"data-field": "seriesId"})
    if not placeholders:
        return None

    lazy_filter: Dict[str, Any] = page_globals.get("lazyLoadFilter") or {}
    fragments: Dict[str, Any] = page_globals.get("fragments") or {}

    # JSON object order is preserved by json.loads, and the captured XHR
    # query strings follow exactly this key order (jQuery serializes the
    # filter object as-is) — keep it, with nulls as empty strings.
    base_query = {key: ("" if value is None else str(value)) for key, value in lazy_filter.items()}

    series: List[Dict[str, Any]] = []
    for placeholder in placeholders:
        term = placeholder.get("data-term")
        if not term:
            continue
        entry = fragments.get(term) or {}
        series.append(
            {
                "series_id": term,
                "frag_sys_id": entry.get("Item1"),
                "stamp": entry.get("Item2"),
            }
        )

    return {
        "content_url": _absolute(page_globals.get("lazyLoadContentUrl") or LAZYLOAD_CONTENT_PATH),
        "toc_sys_id": lazy_filter.get("TocSysId") or page_globals.get("tocSysId"),
        "base_query": base_query,
        "series": series,
    }


def build_lazyload_url(lazyload: Dict[str, Any], series_entry: Dict[str, Any]) -> str:
    """Reconstruct the exact GetLazyLoadContent URL the page's JS would issue.

    Takes the ``lazyload`` plan from :func:`parse_detail_page` and one of its
    ``series`` entries; returns the absolute URL — the serialized
    ``lazyLoadFilter`` with ``SeriesId`` filled, then ``FragSysId`` (Item1)
    and the ``_`` cache-buster (Item2) appended, matching the captured
    requests in tests/fixtures/lazyload_urls.txt.
    """
    frag_sys_id = series_entry.get("frag_sys_id")
    if not frag_sys_id:
        raise ValueError(
            f"series {series_entry.get('series_id')!r} has no FragSysId in the "
            "page's fragments map — cannot build a lazy-load URL for it"
        )
    params = dict(lazyload["base_query"])
    params["SeriesId"] = series_entry["series_id"]
    params["FragSysId"] = frag_sys_id
    params["_"] = series_entry.get("stamp") or ""
    return f"{lazyload['content_url']}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def parse_detail_page(html: str) -> Dict[str, Any]:
    """Parse an Act detail page's initial HTML into the Phase-2 contract dict.

    Returns ``{shorthand, long_title, current_as_at, versions, lazyload,
    toc_entries}`` — see the module docstring for the shape of each field.
    Pure function: no IO, no HTTP.
    """
    soup = BeautifulSoup(html, "lxml")
    page_globals = _page_globals(soup)
    return {
        "shorthand": _parse_shorthand(page_globals),
        "long_title": _parse_long_title(soup),
        "current_as_at": _parse_current_as_at(soup),
        "versions": _parse_versions(soup),
        "lazyload": _parse_lazyload(soup, page_globals),
        "toc_entries": _count_toc_entries(soup),
    }
