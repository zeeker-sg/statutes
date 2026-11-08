"""Parsers for the SSO "AS PUBLISHED" supplement browse pages.

Source:   https://sso.agc.gov.sg/Browse/SL-Supp/Published/{year}
          https://sso.agc.gov.sg/Browse/Acts-Supp/Published/{year}
Fixtures: tests/fixtures/sl_supp_2026.html, tests/fixtures/acts_supp_2026.html
          (saved from the live site 2026-06-11). Pure parsing module — no
          network access; the gazette_stream resource feeds it fetched HTML.

The supplement streams are the event log of the rolling-update architecture
(PRD "Rolling update architecture"): one immutable row per as-published
item, with the sequential gazette number as the monotonic cursor. This
module turns one browse page into `gazette_stream`-shaped dicts and
classifies each title into an event kind so amendments/revocations can
invalidate their principal works in the catalog.

Page anatomy (identical template for both series)
--------------------------------------------------
``table.browse-list > tbody > tr`` — one row per item. Per row:

- first ``td > a.non-ajax`` — title text + detail href containing both the
  work segment and the publication date:
  ``/SL-Supp/S355-2026/Published/20260529?DocDate=20260529``
  ``/Acts-Supp/8-2026/Published/20260427?DocDate=20260427``
- ``td.col-no`` — the gazette number as shown ("S 355/2026" for SL;
  "Act 8 of 2026" for Acts — the "Act " prefix is stripped so the stored
  number matches the PRD's "19 of 2025" citation form).
- ``a.file-download`` — as-published PDF link carrying a ``_`` cache-buster
  param, stripped from the stored ``pdf_url`` (PRD: stamps are
  change-detection state, not identity).

Event classification and principal normalization
-------------------------------------------------
``classify_event`` is deliberately conservative — a WRONG principal match is
worse than none, because an unresolved event is caught by the catalog
reconciliation safety net while a mismatched one invalidates the wrong work.

Order of checks (first match wins):

1. "Corrigend…" anywhere                          -> ``corrigenda``, no principal
2. "(Commencement…)" marker                       -> ``commencement``, no principal
   (checked before amendment: commencement notification titles embed the
   amending Act's full title, e.g. "X (Amendment) Act 2026 (Commencement)
   Notification 2026")
3. "(Revocation)" / "(Revocation No. N)" marker   -> ``revocation`` + principal
4. "(Amendment)" / "(Amendment No. N)" marker, or
   bare "… Amendment Act" (singular)              -> ``amendment`` + principal
5. any other Amend…/Revok…/Revocation/Commencement
   wording (omnibus titles like "… and Other
   Amendments Act", "Statutes (Miscellaneous
   Amendments) Act")                              -> ``other``, no principal
6. everything else (plain new instruments)        -> ``new``, no principal

Principal-title normalization rules (these feed catalog title lookup —
keep in sync with the resolver in gazette_stream.py):

- remove every "(Amendment[ No. N])" / "(Revocation[ No. N])" marker group;
- for the bare form, remove the word "Amendment" before "Act";
- remove an omnibus " and Other Matters"/" and Other Amendments" tail
  before the instrument-type word ("Tobacco … (Amendment) and Other
  Matters Act 2026" -> "Tobacco … Act");
- strip the trailing year ("Road Traffic (Amendment) Act 2026" ->
  "Road Traffic Act" — principals are matched year-insensitively because
  the principal work's own year differs from the amending instrument's);
- collapse internal whitespace. If nothing usable remains, the principal
  is ``None``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

BASE_URL = "https://sso.agc.gov.sg"

#: The two supplement series ingested in this release (PRD: bills_supp later).
SERIES = ("sl_supp", "acts_supp")

# -- gazette numbers ----------------------------------------------------------
# SL: "S 355/2026"; Acts: shown as "Act 19 of 2025", stored as "19 of 2025".
_SL_NUMBER_RE = re.compile(r"\bS\s*(\d+)\s*/\s*(\d{4})\b")
_ACT_NUMBER_RE = re.compile(r"\b(?:Act\s+)?(\d+)\s+of\s+(\d{4})\b")
# URL fallback: /SL-Supp/S355-2026/... and /Acts-Supp/8-2026/...
_SL_URL_NUMBER_RE = re.compile(r"/SL-Supp/S(\d+)-(\d{4})\b")
_ACT_URL_NUMBER_RE = re.compile(r"/Acts-Supp/(\d+)-(\d{4})\b")

_PUBLISHED_DATE_RE = re.compile(r"/Published/(\d{4})(\d{2})(\d{2})\b")

# -- event classification -----------------------------------------------------
_CORRIGENDA_RE = re.compile(r"corrigend", re.IGNORECASE)
_COMMENCEMENT_MARKER_RE = re.compile(r"\(Commencement\b[^)]*\)")
_REVOCATION_MARKER_RE = re.compile(r"\s*\(Revocation(?:\s+No\.?\s*\d+)?\)")
_AMENDMENT_MARKER_RE = re.compile(r"\s*\(Amendment(?:\s+No\.?\s*\d+)?\)")
# Bare "… Amendment Act" (singular — must NOT match omnibus "… and Other
# Amendments Act"; the \b after "Amendment" rejects the plural).
_BARE_AMENDMENT_ACT_RE = re.compile(r"\s+Amendment(?=\s+Act\b)")
# Anything amendment/revocation/commencement-flavoured that the clean
# patterns above did not catch -> "other" (conservative; see module docstring).
_UNSURE_RE = re.compile(r"\b(?:Amend\w*|Revocation|Revok\w*|Commencement)\b", re.IGNORECASE)

_OMNIBUS_TAIL_RE = re.compile(
    r"\s+and\s+Other\s+(?:Matters|Amendments)"
    r"(?=\s+(?:Act|Regulations|Rules|Order|Notification|Proclamation)\b)"
)
_TRAILING_YEAR_RE = re.compile(r"\s+\d{4}$")
_WS_RE = re.compile(r"\s+")


def _collapse_ws(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _strip_stamp_param(url: str) -> str:
    """Drop the ``_`` cache-buster param from a PDF URL, keeping the rest."""
    parts = urlsplit(url)
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "_"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


def _normalize_principal(title: str, marker_re: re.Pattern[str]) -> Optional[str]:
    """Apply the principal-title normalization rules from the module docstring."""
    base = marker_re.sub("", title)
    base = _BARE_AMENDMENT_ACT_RE.sub("", base)
    base = _OMNIBUS_TAIL_RE.sub("", base)
    base = _TRAILING_YEAR_RE.sub("", base)
    base = _collapse_ws(base)
    # Refuse degenerate results: nothing left, no letters, or nothing removed
    # (the marker regex matched the title but substitution was a no-op).
    if not base or not re.search(r"[A-Za-z]", base) or base == _collapse_ws(title):
        return None
    return base


def classify_event(title: str) -> Tuple[str, Optional[str]]:
    """Classify a supplement item title -> ``(event_kind, principal_title)``.

    ``event_kind`` is one of ``new | amendment | revocation | commencement |
    corrigenda | other``. ``principal_title`` is the normalized principal
    work name for amendment/revocation events only (PRD gazette_stream
    schema), ``None`` otherwise — and ``None`` whenever extraction would be
    a guess. Exposed separately from ``parse_supp_page`` for reuse and
    table-driven tests.
    """
    title = _collapse_ws(title)
    if _CORRIGENDA_RE.search(title):
        return "corrigenda", None
    if _COMMENCEMENT_MARKER_RE.search(title):
        return "commencement", None
    if _REVOCATION_MARKER_RE.search(title):
        return "revocation", _normalize_principal(title, _REVOCATION_MARKER_RE)
    if _AMENDMENT_MARKER_RE.search(title) or _BARE_AMENDMENT_ACT_RE.search(title):
        return "amendment", _normalize_principal(title, _AMENDMENT_MARKER_RE)
    if _UNSURE_RE.search(title):
        return "other", None
    return "new", None


def _parse_number(text: str, series: str) -> Optional[Tuple[str, int, int]]:
    """Parse a gazette number -> ``(number, number_seq, year)`` or ``None``."""
    if series == "sl_supp":
        m = _SL_NUMBER_RE.search(text)
        if m:
            return f"S {int(m.group(1))}/{m.group(2)}", int(m.group(1)), int(m.group(2))
        return None
    m = _ACT_NUMBER_RE.search(text)
    if m:
        return f"{int(m.group(1))} of {m.group(2)}", int(m.group(1)), int(m.group(2))
    return None


def _number_from_row(row: Any, source_url: str, series: str) -> Tuple[str, int, int]:
    """Gazette number from the row's Number cell, with URL-segment fallback."""
    cell = row.select_one("td.col-no")
    if cell is not None:
        parsed = _parse_number(cell.get_text(" ", strip=True), series)
        if parsed:
            return parsed
    url_re = _SL_URL_NUMBER_RE if series == "sl_supp" else _ACT_URL_NUMBER_RE
    m = url_re.search(source_url)
    if m:
        seq, year = int(m.group(1)), m.group(2)
        number = f"S {seq}/{year}" if series == "sl_supp" else f"{seq} of {year}"
        return number, seq, int(year)
    raise ValueError(f"no gazette number found for supplement row at {source_url!r}")


def _published_date(source_url: str) -> str:
    """ISO date from the ``/Published/{YYYYMMDD}`` URL segment."""
    m = _PUBLISHED_DATE_RE.search(source_url)
    if not m:
        raise ValueError(f"no /Published/{{date}} segment in supplement URL {source_url!r}")
    return "-".join(m.groups())


def parse_supp_page(html: str, series: str) -> List[Dict[str, Any]]:
    """Parse one supplement browse page into gazette_stream-shaped dicts.

    Args:
        html: full HTML of a ``/Browse/SL-Supp/Published/{year}`` or
            ``/Browse/Acts-Supp/Published/{year}`` page.
        series: ``"sl_supp"`` or ``"acts_supp"`` — recorded on every row and
            selects the gazette-number grammar.

    Returns:
        One dict per item row, in document order, with keys: ``series``,
        ``number`` ("S 355/2026" / "19 of 2025"), ``number_seq`` (int part
        of the number — the cursor), ``year``, ``title``,
        ``published_date`` (ISO, from the URL segment), ``source_url``
        (absolute), ``pdf_url`` (absolute, ``_`` param stripped; ``None``
        if the row has no PDF), ``event_kind``, ``principal_title``.

    Raises:
        ValueError: unknown ``series``, or a row missing its gazette number
            or publication date — identity fields fail loudly (PRD tripwire
            philosophy) rather than minting a wrong cursor.
    """
    if series not in SERIES:
        raise ValueError(f"series must be one of {SERIES}, got {series!r}")

    soup = BeautifulSoup(html, "lxml")
    items: List[Dict[str, Any]] = []
    for row in soup.select("table.browse-list tbody tr"):
        title_link = row.select_one("td > a.non-ajax[href]")
        if title_link is None:  # defensive: not an item row
            continue
        title = _collapse_ws(title_link.get_text(" ", strip=True))
        source_url = urljoin(BASE_URL, title_link["href"])
        number, number_seq, year = _number_from_row(row, source_url, series)

        pdf_link = row.select_one("a.file-download[href]")
        pdf_url = None
        if pdf_link is not None:
            pdf_url = _strip_stamp_param(urljoin(BASE_URL, pdf_link["href"]))

        event_kind, principal_title = classify_event(title)
        items.append(
            {
                "series": series,
                "number": number,
                "number_seq": number_seq,
                "year": year,
                "title": title,
                "published_date": _published_date(source_url),
                "source_url": source_url,
                "pdf_url": pdf_url,
                "event_kind": event_kind,
                "principal_title": principal_title,
            }
        )
    return items
