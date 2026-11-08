"""Fixture-driven tests for resources/parse_provisions.py (no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

import ids
import parse_provisions
import pytest
from bs4 import BeautifulSoup

FIXTURES = Path(__file__).resolve().parent / "fixtures"

IA1965_PART2_SERIES = "fada5b0d-3f25-4447-bfbf-5462c9466e98"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _assemble_with_stubs(detail_html: str, series_html: Optional[Dict[str, str]] = None) -> str:
    """Assemble with real payloads where given and empty-div stubs elsewhere.

    ``assemble_body`` refuses missing/empty payloads by design (a partial
    body must never parse as if complete), so tests that captured only some
    series stub the rest with a content-free response.
    """
    series_html = dict(series_html or {})
    soup = BeautifulSoup(detail_html, "lxml")
    for placeholder in soup.find_all("div", class_="dms", attrs={"data-field": "seriesId"}):
        series_html.setdefault(placeholder.get("data-term") or "", "<div></div>")
    return parse_provisions.assemble_body(detail_html, series_html)


@pytest.fixture(scope="module")
def part2_fragments():
    """The captured IA1965 Part 2 lazy-load response (19 sections)."""
    return parse_provisions.parse_provisions(_fixture("lazyload_response.html"), "IA1965")


@pytest.fixture(scope="module")
def ia1965_fragments():
    """Initial IA1965 HTML assembled with the one captured body series."""
    body = _assemble_with_stubs(
        _fixture("act_IA1965_raw.html"),
        {IA1965_PART2_SERIES: _fixture("lazyload_response.html")},
    )
    return parse_provisions.parse_provisions(body, "IA1965")


# =============================================================================
# Sections, headings, part context
# =============================================================================
def test_part2_series_yields_19_sections(part2_fragments):
    assert len(part2_fragments) == 19
    assert all(f["unit_kind"] == "section" for f in part2_fragments)


def test_part_context_and_provision_ids(part2_fragments):
    s3 = part2_fragments[0]
    assert s3["provision_id"] == "IA1965/pt2/s3"
    assert s3["provision_num"] == "3"
    assert s3["heading"] == "Acts to be public Acts"
    assert s3["part_num"] == "2"
    assert s3["part_heading"] == "GENERAL PROVISIONS REGARDING WRITTEN LAW"


def test_letter_suffixed_provision_numbers(part2_fragments):
    nums = [f["provision_num"] for f in part2_fragments]
    assert "7A" in nums and "9A" in nums and "9B" in nums
    s9a = next(f for f in part2_fragments if f["provision_num"] == "9A")
    assert s9a["heading"].startswith("Purposive interpretation")
    assert "interpretation that would promote the purpose" in s9a["content_text"]


def test_seq_is_document_order(part2_fragments):
    assert [f["seq"] for f in part2_fragments] == list(range(19))


# =============================================================================
# Flattening: structural labels, newlines, structure_json offsets
# =============================================================================
def test_structural_labels_kept_verbatim_with_newlines(part2_fragments):
    s8 = next(f for f in part2_fragments if f["provision_num"] == "8")
    lines = s8["content_text"].split("\n")
    assert lines[0].startswith("8.")
    assert any(line.startswith("(a)") for line in lines)
    assert any(line.startswith("(i)") for line in lines)
    assert any(line.startswith("(2)") for line in lines)


def test_structure_json_offsets_roundtrip(part2_fragments):
    """PRD definition of done: offsets slice content_text back exactly."""
    s8 = next(f for f in part2_fragments if f["provision_num"] == "8")
    text = s8["content_text"]
    nodes = json.loads(s8["structure_json"])
    subsections = [n for n in nodes if n["kind"] == "subsection"]
    assert [n["num"] for n in subsections] == ["1", "2", "3", "4"]
    ss2 = subsections[1]
    assert text[ss2["char_start"] : ss2["char_end"]].startswith(
        "(2) A reference for the purposes of subsection (1)"
    )
    ss1_paras = [n for n in subsections[0]["children"] if n["kind"] == "paragraph"]
    assert [n["num"] for n in ss1_paras] == ["a", "b"]
    sub_paras = [n for n in ss1_paras[0]["children"] if n["kind"] == "subparagraph"]
    assert [n["num"] for n in sub_paras] == ["i", "ii", "iii"]
    sp = sub_paras[1]
    assert text[sp["char_start"] : sp["char_end"]].startswith("(ii) where the Act")


def test_amend_notes_excluded_from_text_and_captured(part2_fragments):
    s8 = next(f for f in part2_fragments if f["provision_num"] == "8")
    assert "[4/2021]" not in s8["content_text"]
    assert "[4/2021]" in s8["source_notes"]
    assert "[Act 5 of 2025 wef 09/03/2025]" in s8["source_notes"]


# =============================================================================
# Assembly, long title, oversize splitting (assembled IA1965)
# =============================================================================
def test_assembled_act_has_longtitle_first(ia1965_fragments):
    longtitle = ia1965_fragments[0]
    assert longtitle["provision_id"] == "IA1965/longtitle"
    assert longtitle["unit_kind"] == "longtitle"
    assert longtitle["seq"] == 0
    assert longtitle["content_text"].startswith("An Act to define certain terms")


def test_assembled_act_merges_inline_and_lazyloaded_series(ia1965_fragments):
    provision_ids = [f["provision_id"] for f in ia1965_fragments]
    assert "IA1965/s1" in provision_ids  # inline in the initial HTML
    assert "IA1965/pt2/s9A" in provision_ids  # from the lazy-load response


def test_assemble_body_raises_on_missing_series_payload():
    """An unreplaced dms placeholder = partial statutory text. assemble_body
    must refuse, so the act is quarantined instead of extracted incomplete."""
    with pytest.raises(ValueError, match="unreplaced dms placeholder"):
        parse_provisions.assemble_body(_fixture("act_IA1965_raw.html"), {})


def test_assemble_body_raises_on_empty_series_payload():
    html = (
        '<html><body><div class="body">'
        '<div class="dms" data-field="seriesId" data-term="series-1"></div>'
        "</div></body></html>"
    )
    with pytest.raises(ValueError, match="series 'series-1'"):
        parse_provisions.assemble_body(html, {"series-1": "   "})


def test_assemble_body_replaces_every_placeholder_when_payloads_complete():
    body = _assemble_with_stubs(_fixture("act_IA1965_raw.html"))
    assert 'class="dms"' not in body


def test_oversize_definitions_section_splits_structurally(ia1965_fragments):
    chunks = [f for f in ia1965_fragments if f["provision_id"].startswith("IA1965/pt1/s2/")]
    assert len(chunks) >= 2
    assert [f["provision_id"] for f in chunks] == [
        f"IA1965/pt1/s2/c{i}" for i in range(1, len(chunks) + 1)
    ]
    max_chars = parse_provisions.max_fragment_chars()
    for chunk in chunks:
        # Self-contained: breadcrumbed first line, under the cap.
        assert chunk["content_text"].startswith(
            "2. Interpretation of certain words and expressions"
        )
        assert len(chunk["content_text"]) <= max_chars + 100  # breadcrumb allowance
        assert chunk["unit_kind"] == "section"
        assert chunk["provision_num"] == "2"
    # Re-offset structure_json still slices the chunk text exactly.
    first = chunks[0]
    nodes = json.loads(first["structure_json"])
    assert nodes, "split chunks keep their structural nodes"
    abet = nodes[0]
    assert abet["kind"] == "definition"
    assert abet["num"] == "abet"
    sliced = first["content_text"][abet["char_start"] : abet["char_end"]]
    assert sliced.startswith("“abet”")


def test_no_fragment_exceeds_the_cap_after_split(ia1965_fragments):
    max_chars = parse_provisions.max_fragment_chars()
    oversize = [f for f in ia1965_fragments if len(f["content_text"]) > max_chars + 100]
    assert oversize == []


def test_split_is_structural_with_tiny_cap():
    fragments = parse_provisions.parse_provisions(
        _fixture("lazyload_response.html"), "IA1965", max_chars=400
    )
    s8_chunks = [f for f in fragments if f["provision_id"].startswith("IA1965/pt2/s8/")]
    assert len(s8_chunks) >= 2
    for chunk in s8_chunks:
        body = chunk["content_text"].split("\n", 1)[1]
        # Structural boundaries: every chunk body starts at a label or the chapeau.
        assert body[0] in "8(" or body.startswith("—")


# =============================================================================
# Short Act (AA1928) and schedules (EA1893)
# =============================================================================
def test_short_act_parses_inline_without_parts():
    body = _assemble_with_stubs(_fixture("act_AA1928_raw.html"))
    fragments = parse_provisions.parse_provisions(body, "AA1928")
    assert [f["provision_id"] for f in fragments] == [
        "AA1928/longtitle",
        *[f"AA1928/s{n}" for n in range(1, 8)],
    ]
    assert all(f["part_num"] is None for f in fragments)
    s3 = next(f for f in fragments if f["provision_num"] == "3")
    assert "accruing from day to day" in s3["content_text"]


def test_schedules_become_schedule_fragments():
    fragments = parse_provisions.parse_provisions(_fixture("act_EA1893_rendered.html"), "EA1893")
    schedules = [f for f in fragments if f["unit_kind"] == "schedule"]
    assert [s["provision_id"] for s in schedules] == ["EA1893/sch1", "EA1893/sch2"]
    assert schedules[0]["heading"] == "FIRST SCHEDULE"
    assert "Child abuse offences" in schedules[0]["content_text"]
    # Schedule content is not double-counted as sections.
    section_ids = [f["provision_id"] for f in fragments if f["unit_kind"] == "section"]
    assert len(section_ids) == len(set(section_ids))


# =============================================================================
# Fragment ids stay unique per work (tripwire precondition)
# =============================================================================
def test_provision_ids_unique_within_each_work(ia1965_fragments):
    provision_ids = [f["provision_id"] for f in ia1965_fragments]
    assert len(provision_ids) == len(set(provision_ids))
    hashes = {ids.fragment_id("abc123def456", p) for p in provision_ids}
    assert len(hashes) == len(provision_ids)


def test_unparseable_section_numbers_get_distinct_document_order_ids():
    """Two number-less sections must never collide into one .../sNone
    provision_id (identical fragment-id hashes = silent row loss on the
    (parent_id, provision_id) unique index). The fallback is deterministic
    document order: unnum1, unnum2, ..."""
    html = """<html><body><div class="body">
    <div class="prov1">
      <table><tr><td class="prov1Hdr"><span>First unnumbered heading</span></td></tr></table>
      <table><tr><td class="prov1Txt">Text without any leading number marker.</td></tr></table>
    </div>
    <div class="prov1">
      <table><tr><td class="prov1Hdr"><span>Second unnumbered heading</span></td></tr></table>
      <table><tr><td class="prov1Txt">More text, also without a number.</td></tr></table>
    </div>
    </div></body></html>"""
    fragments = parse_provisions.parse_provisions(html, "XX2026")
    assert [f["provision_num"] for f in fragments] == [None, None]
    assert [f["provision_id"] for f in fragments] == ["XX2026/unnum1", "XX2026/unnum2"]
    assert len({ids.fragment_id("abc123def456", f["provision_id"]) for f in fragments}) == 2
