"""Provision-level parser for SSO Act body HTML (PRD Phase 2, step 2 — no IO).

Input is the *assembled* body HTML — the initial detail page with every
``div.dms`` lazy-load placeholder replaced by its fetched series response
(see :func:`assemble_body`) — or any HTML carrying the same markup (a single
lazy-load response, a rendered fixture). Output is one fragment dict per
provision, the ``acts_fragments`` unit (PRD: "Fragment unit = the section").

Markup anatomy (verified against the committed fixtures)
---------------------------------------------------------
- Section: ``div.prov1`` → header ``td.prov1Hdr`` (id ``pr{NUM}-`` carries
  the provision number; text is the heading) + body ``td.prov1Txt``
  (leading ``<strong>{NUM}.</strong>`` then the section text).
- Part context: sections nest inside ``td.part`` (id ``P1{n}-``) with a
  ``div.partNo`` ("PART 2") and ``td.partHdr`` heading.
- Sub-structure: subsections are ``td.prov2Txt`` rows (or an inline
  ``span.prov2TxtIL`` for "—(1) ..." run into the section's first line);
  paragraphs are ``td.p1No``/``td.pTxt`` row pairs; sub-paragraphs
  ``td.p2No``/``td.p2Txt``; definitions are ``td.def`` lines (one per
  defined term, with ``td.p1DefNo``/``td.pDefTxt`` paragraph pairs inside);
  ``td.prov1N2SO`` / ``td.defSO`` / ``td.prov2DefSO`` are unnumbered
  continuation lines.
- Amendment-history annotations are ``div.amendNote`` ("[Act 5 of 2025 wef
  09/03/2025]"); they are captured into ``source_notes`` and excluded from
  ``content_text`` (PRD: keep FTS clean).
- Schedules: ``div.schedule`` with a ``td.sHdr`` heading; flattened whole
  (per-schedule fragments; finer schedule-paragraph splitting is deferred).

Flattening rules (PRD ``acts_fragments`` design)
------------------------------------------------
``content_text`` keeps every structural label verbatim — ``(1)``, ``(a)``,
``(i)`` — with a newline at every structural boundary. ``structure_json``
is a JSON array of the parsed subtree per fragment
(``{num, kind, char_start, char_end, children}``) whose character offsets
slice ``content_text`` back into the exact subsection/paragraph texts.

Oversize rule: a section whose flattened text exceeds
``STATUTES_MAX_FRAGMENT_CHARS`` (default 9000) splits at structural
boundaries — consecutive top-level units grouped under the limit, recursing
one level down when a single unit (e.g. the definitions subsection of
IA1965 s 2) is itself oversize. Child fragments get ``provision_id``
``{section_id}/c{n}``, a breadcrumb first line (section number + heading)
so each stays self-contained, and re-offset ``structure_json``. Splits are
structural, never blind character windows.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, NavigableString
from bs4.element import Tag

DEFAULT_MAX_FRAGMENT_CHARS = 9000

#: Number cells whose text becomes the "(a)" / "(i)" prefix of the next line.
_NUM_CLASSES = {"p1No", "p2No", "p1DefNo", "sIndentNo"}

#: td classes that open a structural node: class -> (kind, depth).
_LINE_NODE_KINDS: Dict[str, Tuple[str, int]] = {
    "prov2Txt": ("subsection", 1),
    "def": ("definition", 2),
    "pTxt": ("paragraph", 2),
    "pIndentTxt": ("paragraph", 2),
    "pDefTxt": ("paragraph", 3),
    "p2Txt": ("subparagraph", 3),
}

_WS_RE = re.compile(r"\s+")
_HDR_ID_NUM_RE = re.compile(r"^pr(\w+)-")
_LEADING_NUM_RE = re.compile(r"^(\w+)\.")
_PART_NO_RE = re.compile(r"PART\s+(\w+)", re.IGNORECASE)
_LABEL_NUM_RE = re.compile(r"^[—–-]*\s*\((\w+)\)")
_DEF_TERM_RE = re.compile(r"^[“\"]([^”\"]+)[”\"]")


def _collapse(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _norm_label(text: str) -> str:
    """Normalise a number-cell label: ``( a )`` -> ``(a)``."""
    text = _collapse(text)
    text = re.sub(r"\(\s*", "(", text)
    return re.sub(r"\s*\)", ")", text)


def max_fragment_chars() -> int:
    """Oversize threshold, env-overridable via ``STATUTES_MAX_FRAGMENT_CHARS``."""
    return int(os.environ.get("STATUTES_MAX_FRAGMENT_CHARS", str(DEFAULT_MAX_FRAGMENT_CHARS)))


# ---------------------------------------------------------------------------
# Body assembly (initial HTML + lazy-load responses -> one document)
# ---------------------------------------------------------------------------
def assemble_body(detail_html: str, series_html: Dict[str, str]) -> str:
    """Replace each ``div.dms`` placeholder with its fetched series content.

    ``series_html`` maps SeriesId (the placeholder's ``data-term``) to the
    raw HTML of its GetLazyLoadContent response, exactly as the site's own
    JS inserts it. Returns the merged document HTML.

    Raises ``ValueError`` if any placeholder would remain unreplaced — a
    series missing from ``series_html`` or an empty payload means the
    assembled body would silently omit statutory text, and the caller must
    treat the whole act as an extraction failure (ToU cl 13(b): we own
    accuracy; a partial body must never be marked extracted).
    """
    soup = BeautifulSoup(detail_html, "lxml")
    for placeholder in soup.find_all("div", class_="dms", attrs={"data-field": "seriesId"}):
        term = placeholder.get("data-term") or ""
        payload = series_html.get(term)
        if not payload or not payload.strip():
            raise ValueError(
                f"unreplaced dms placeholder for series {term!r}: lazy-load payload "
                "missing or empty — refusing to assemble a partial act body"
            )
        parsed = BeautifulSoup(payload, "lxml")
        container = parsed.body or parsed
        placeholder.replace_with(*list(container.children))
    return str(soup)


# ---------------------------------------------------------------------------
# Flattener: markup -> (content_text, structure nodes, source notes)
# ---------------------------------------------------------------------------
class _Flattener:
    """Walks provision markup, emitting one text line per structural row."""

    def __init__(self) -> None:
        self.text = ""
        self.notes: List[str] = []
        self._flat_nodes: List[Dict[str, Any]] = []
        self._parts: List[str] = []
        self._pending_prefix = ""
        self._pending_node: Optional[Tuple[str, int]] = None
        self._inline_marks: List[Tuple[str, int, int]] = []

    # -- line plumbing -----------------------------------------------------
    def _line_start_pos(self) -> int:
        return len(self.text) + 1 if self.text else 0

    def _buffered_len(self) -> int:
        return len(_collapse("".join(self._parts)))

    def _mark_inline(self, kind: str, depth: int) -> None:
        """Open a node mid-line (``span.prov2TxtIL``: "—(1) ..." inline)."""
        buffered = self._buffered_len()
        offset = buffered + 1 if buffered else 0
        self._inline_marks.append((kind, depth, offset))

    def flush(self) -> None:
        line = _collapse("".join(self._parts))
        self._parts = []
        node = self._pending_node
        self._pending_node = None
        marks = self._inline_marks
        self._inline_marks = []
        if self._pending_prefix:
            line = f"{self._pending_prefix} {line}".strip() if line else self._pending_prefix
            self._pending_prefix = ""
        if not line:
            # Nothing to emit; keep nothing (prefix/marks for empty rows drop).
            return
        start = self._line_start_pos()
        if node is not None:
            kind, depth = node
            self._add_node(kind, depth, start, line)
        for kind, depth, offset in marks:
            self._add_node(kind, depth, start + offset, line[offset:])
        self.text = f"{self.text}\n{line}" if self.text else line

    def _add_node(self, kind: str, depth: int, char_start: int, line: str) -> None:
        num: Optional[str] = None
        if kind == "definition":
            term = _DEF_TERM_RE.match(line)
            num = term.group(1) if term else None
        else:
            label = _LABEL_NUM_RE.match(line)
            num = label.group(1) if label else None
        self._flat_nodes.append(
            {"kind": kind, "depth": depth, "num": num, "char_start": char_start}
        )

    # -- markup walk ---------------------------------------------------------
    def walk(self, element: Tag) -> None:
        for child in element.children:
            if isinstance(child, NavigableString):
                self._parts.append(str(child))
                continue
            if not isinstance(child, Tag):
                continue
            classes = set(child.get("class") or ())
            if "amendNote" in classes:
                note = _collapse(child.get_text(" ", strip=True))
                if note and note not in self.notes:
                    self.notes.append(note)
                continue
            if child.name in ("table", "tbody", "thead", "tr"):
                self.flush()
                self.walk(child)
                self.flush()
                continue
            if child.name == "td":
                if classes & _NUM_CLASSES:
                    self.flush()
                    self._pending_prefix = _norm_label(child.get_text(" ", strip=True))
                    continue
                node_kinds = [_LINE_NODE_KINDS[c] for c in classes if c in _LINE_NODE_KINDS]
                if node_kinds:
                    self._pending_node = node_kinds[0]
                self.walk(child)
                self.flush()
                continue
            if child.name == "span" and "prov2TxtIL" in classes:
                self._mark_inline("subsection", 1)
                self.walk(child)
                continue
            # Inline tag (strong/em/a/span/div without structure): text only,
            # unless it secretly contains structural rows.
            if child.find(["table", "tr", "td"]) is not None:
                self.walk(child)
            else:
                self._parts.append(child.get_text())

    # -- results -------------------------------------------------------------
    def nodes(self) -> List[Dict[str, Any]]:
        """Finalised structure tree (ends computed, nested by depth)."""
        flat = self._flat_nodes
        text_len = len(self.text)
        for i, node in enumerate(flat):
            end = text_len
            for later in flat[i + 1 :]:
                if later["depth"] <= node["depth"]:
                    end = later["char_start"]
                    break
            node["char_end"] = end
        root: List[Dict[str, Any]] = []
        stack: List[Tuple[int, Dict[str, Any]]] = []
        for node in flat:
            built = {
                "num": node["num"],
                "kind": node["kind"],
                "char_start": node["char_start"],
                "char_end": node["char_end"],
                "children": [],
            }
            while stack and stack[-1][0] >= node["depth"]:
                stack.pop()
            (stack[-1][1]["children"] if stack else root).append(built)
            stack.append((node["depth"], built))
        return root


def _flatten(elements: List[Tag]) -> Tuple[str, List[Dict[str, Any]], Optional[str]]:
    flattener = _Flattener()
    for element in elements:
        flattener.walk(element)
        flattener.flush()
    notes = "; ".join(flattener.notes) if flattener.notes else None
    return flattener.text, flattener.nodes(), notes


# ---------------------------------------------------------------------------
# Oversize splitting (structural, never blind character windows)
# ---------------------------------------------------------------------------
def _structural_spans(
    nodes: List[Dict[str, Any]], seg_start: int, seg_end: int, max_chars: int
) -> List[Tuple[int, int]]:
    """Split [seg_start, seg_end) at node boundaries into spans <= max_chars.

    Greedily merges consecutive pieces; a single oversize node recurses into
    its children, and an unsplittable oversize piece is kept whole.
    """
    boundaries = [n["char_start"] for n in nodes if seg_start < n["char_start"] < seg_end]
    pieces: List[Tuple[int, int, Optional[Dict[str, Any]]]] = []
    cuts = [seg_start, *boundaries, seg_end]
    node_at = {n["char_start"]: n for n in nodes}
    for left, right in zip(cuts, cuts[1:]):
        if left < right:
            pieces.append((left, right, node_at.get(left)))

    spans: List[Tuple[int, int]] = []
    run_start: Optional[int] = None
    run_end = 0
    for left, right, node in pieces:
        if right - left > max_chars and node is not None and node["children"]:
            if run_start is not None:
                spans.append((run_start, run_end))
                run_start = None
            spans.extend(_structural_spans(node["children"], left, right, max_chars))
            continue
        if run_start is None:
            run_start, run_end = left, right
        elif right - run_start <= max_chars:
            run_end = right
        else:
            spans.append((run_start, run_end))
            run_start, run_end = left, right
    if run_start is not None:
        spans.append((run_start, run_end))
    # Coalesce pass: recursion above can leave adjacent spans (e.g. a tiny
    # chapeau before an oversize subsection) that fit together under the cap.
    merged: List[Tuple[int, int]] = []
    for span in spans:
        if merged and span[1] - merged[-1][0] <= max_chars:
            merged[-1] = (merged[-1][0], span[1])
        else:
            merged.append(span)
    return merged


def _nodes_within(nodes: List[Dict[str, Any]], start: int, end: int) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    for node in nodes:
        if node["char_start"] >= end or node["char_end"] <= start:
            continue
        if node["char_start"] >= start and node["char_end"] <= end:
            selected.append(node)
        else:
            selected.extend(_nodes_within(node["children"], start, end))
    return selected


def _shift_nodes(nodes: List[Dict[str, Any]], delta: int) -> List[Dict[str, Any]]:
    return [
        {
            "num": n["num"],
            "kind": n["kind"],
            "char_start": n["char_start"] + delta,
            "char_end": n["char_end"] + delta,
            "children": _shift_nodes(n["children"], delta),
        }
        for n in nodes
    ]


def _split_fragment(
    fragment: Dict[str, Any], nodes: List[Dict[str, Any]], max_chars: int
) -> List[Dict[str, Any]]:
    text = fragment["content_text"]
    spans = _structural_spans(nodes, 0, len(text), max_chars)
    if len(spans) <= 1:
        return [fragment]
    heading = fragment.get("heading") or ""
    num = fragment.get("provision_num") or ""
    breadcrumb = _collapse(f"{num}. {heading}" if num else heading)
    children: List[Dict[str, Any]] = []
    for index, (start, end) in enumerate(spans, 1):
        chunk = text[start:end].strip()
        content = f"{breadcrumb}\n{chunk}" if breadcrumb else chunk
        delta = (len(breadcrumb) + 1 if breadcrumb else 0) - start
        chunk_nodes = _shift_nodes(_nodes_within(nodes, start, end), delta)
        child = dict(fragment)
        child["provision_id"] = f"{fragment['provision_id']}/c{index}"
        child["content_text"] = content
        child["structure_json"] = json.dumps(chunk_nodes, ensure_ascii=False)
        children.append(child)
    return children


# ---------------------------------------------------------------------------
# Fragment extraction
# ---------------------------------------------------------------------------
def _part_context(unit: Tag) -> Tuple[Optional[str], Optional[str]]:
    part = unit.find_parent(class_="part")
    if part is None:
        return None, None
    part_num = None
    number_div = part.find(class_="partNo")
    if number_div is not None:
        match = _PART_NO_RE.search(number_div.get_text(" ", strip=True))
        if match:
            part_num = match.group(1)
    heading_cell = part.find("td", class_="partHdr")
    part_heading = _collapse(heading_cell.get_text(" ", strip=True)) if heading_cell else None
    return part_num, part_heading or None


def _section_fragment(
    unit: Tag, shorthand: str
) -> Optional[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    header = unit.find("td", class_="prov1Hdr")
    heading = _collapse(header.get_text(" ", strip=True)) if header is not None else None
    num: Optional[str] = None
    if header is not None and header.get("id"):
        match = _HDR_ID_NUM_RE.match(header["id"])
        if match:
            num = match.group(1)

    body_tables = []
    for table in unit.find_all("table", recursive=False):
        if table.find("td", class_="prov1Hdr") is not None:
            continue
        body_tables.append(table)
    text, nodes, notes = _flatten(body_tables)
    if not text and not heading:
        return None
    if num is None:
        match = _LEADING_NUM_RE.match(text)
        if match:
            num = match.group(1)

    part_num, part_heading = _part_context(unit)
    base = f"{shorthand}/pt{part_num}/s{num}" if part_num else f"{shorthand}/s{num}"
    fragment = {
        "provision_id": base,
        "provision_num": num,
        "unit_kind": "section",
        "heading": heading,
        "part_num": part_num,
        "part_heading": part_heading,
        "content_text": text,
        "structure_json": json.dumps(nodes, ensure_ascii=False) if nodes else None,
        "source_notes": notes,
    }
    return fragment, nodes


def _schedule_fragment(
    unit: Tag, shorthand: str, ordinal: int
) -> Optional[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    heading_cell = unit.find("td", class_="sHdr")
    heading = _collapse(heading_cell.get_text(" ", strip=True)) if heading_cell else None
    text, nodes, notes = _flatten([unit])
    if not text:
        return None
    fragment = {
        "provision_id": f"{shorthand}/sch{ordinal}",
        "provision_num": None,
        "unit_kind": "schedule",
        "heading": heading,
        "part_num": None,
        "part_heading": None,
        "content_text": text,
        "structure_json": json.dumps(nodes, ensure_ascii=False) if nodes else None,
        "source_notes": notes,
    }
    return fragment, nodes


def parse_provisions(
    html: str, shorthand: str, max_chars: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Parse assembled Act body HTML into ``acts_fragments``-shaped dicts.

    Returns one dict per fragment, in document order, with keys:
    ``provision_id`` (``IA1965/longtitle``, ``IA1965/pt2/s9A``,
    ``IA1965/sch1``; ``…/c{n}`` for oversize-split children),
    ``provision_num``, ``unit_kind`` (``longtitle | section | schedule``),
    ``heading``, ``part_num``, ``part_heading``, ``seq`` (0-based document
    order), ``content_text``, ``structure_json``, ``source_notes``.
    Primary keys are NOT minted here — the resource hashes
    ``{parent_id}:{provision_id}`` per the PRD identity rules.

    Sections whose number cannot be parsed get a deterministic
    document-order fallback id (``{shorthand}/unnum{n}``, n counting
    unnumbered sections from 1) instead of a shared ``…/sNone`` — two
    number-less sections must never collide into one ``(parent_id,
    provision_id)`` identity (the unique-index tripwire) or one
    fragment-id hash (silent row loss on upsert).
    """
    if max_chars is None:
        max_chars = max_fragment_chars()
    soup = BeautifulSoup(html, "lxml")
    fragments: List[Dict[str, Any]] = []

    long_title_cell = soup.find("td", class_="longTitle")
    if long_title_cell is not None:
        long_title = _collapse(long_title_cell.get_text(" ", strip=True))
        if long_title:
            fragments.append(
                {
                    "provision_id": f"{shorthand}/longtitle",
                    "provision_num": None,
                    "unit_kind": "longtitle",
                    "heading": None,
                    "part_num": None,
                    "part_heading": None,
                    "content_text": long_title,
                    "structure_json": None,
                    "source_notes": None,
                }
            )

    schedule_ordinal = 0
    unnumbered_ordinal = 0
    units = soup.find_all(
        lambda tag: tag.name == "div"
        and tag.has_attr("class")
        and ({"prov1", "schedule"} & set(tag["class"]))
    )
    for unit in units:
        if "prov1" in unit["class"]:
            if unit.find_parent("div", class_="schedule") is not None:
                continue  # flattened as part of its schedule
            built = _section_fragment(unit, shorthand)
        else:
            schedule_ordinal += 1
            built = _schedule_fragment(unit, shorthand, schedule_ordinal)
        if built is None:
            continue
        fragment, nodes = built
        if fragment["unit_kind"] == "section" and fragment["provision_num"] is None:
            # Deterministic document-order fallback (see docstring): never let
            # two number-less sections share an ".../sNone" identity.
            unnumbered_ordinal += 1
            prefix = f"{shorthand}/pt{fragment['part_num']}" if fragment["part_num"] else shorthand
            fragment["provision_id"] = f"{prefix}/unnum{unnumbered_ordinal}"
        if len(fragment["content_text"]) > max_chars:
            fragments.extend(_split_fragment(fragment, nodes, max_chars))
        else:
            fragments.append(fragment)

    for seq, fragment in enumerate(fragments):
        fragment["seq"] = seq
    return fragments
