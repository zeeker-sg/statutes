"""End-to-end offline build: the three resources against fixture-served HTTP.

``zeeker build`` cannot inject transports, so this test reproduces zeeker's
build loop exactly (see zeeker.core.database.processor/builder):

- resources run in zeeker.toml order: gazette_stream first (stream ingestion
  + principal invalidation), then acts (+ acts_fragments: reconciliation and
  the queue drain, so tonight's invalidations re-extract the SAME night),
  then act_versions (reads the extraction cache acts writes);
- each phase loads a FRESH module via ``spec_from_file_location`` (zeeker
  reloads between the main-table and fragments phases — the disk cache must
  bridge it);
- ``fetch_data(existing_table)`` results are inserted with
  ``insert_all(rows, replace=False)`` (no pk — exactly what zeeker does);
- fragments-enabled resources get a SECOND ``fetch_data`` call purely for
  context (never inserted), then
  ``fetch_fragments_data(existing_fragments_table, ctx)``. ``run_build``
  asserts that second call issues ZERO HTTP requests and extracts nothing
  (the per-build marker), and that every real build crawls the listing.

Every HTTP request is served by an ``httpx.MockTransport`` from committed
fixtures (acts without a fixture detail page get a synthesized minimal one),
so the suite runs with zero network. The scenario sequence asserts the PRD
definition-of-done invariants:

1. full build: ~525 catalog rows, AA1928 extracted with fragments and
   FTS-able text, act_versions populated, gazette_stream rows present —
   and a SINGLE from-scratch build already satisfies every sanity-check
   invariant (unique-index tripwires included: the S3 DB is the only
   durable state, so every night is potentially a "first build");
2. second run with unchanged fixtures is a no-op (row counts identical);
3. a mutated listing_stamp re-queues exactly that one Act;
4. the next run re-extracts exactly the re-queued Act;
5. one supplement item above the cursor adds exactly one stream row, and an
   "(Amendment)" title resolving to a catalog Act invalidates that Act —
   which, with gazette_stream running first, is re-extracted the SAME
   night (PRD "Rolling update architecture").
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Dict

import httpx
import pytest
import sqlite_utils

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESOURCES_DIR = PROJECT_ROOT / "resources"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

sys.path.insert(0, str(RESOURCES_DIR))
import ids  # noqa: E402

#: The one IA1965 body series whose lazy-load response was captured.
IA1965_PART2_SERIES = "fada5b0d-3f25-4447-bfbf-5462c9466e98"

#: IA1965's PDF link in the listing — unique to its row (the bare stamp value
#: is shared with HSAA2001, so mutation targets the full link).
IA1965_PDF_LINK = "/Act/IA1965?ViewType=Pdf&amp;_=20260602205225"
IA1965_PDF_LINK_MUTATED = "/Act/IA1965?ViewType=Pdf&amp;_=20260611031500"

NEW_SUPP_ROW = (
    '<tr><td><a class="non-ajax" href="/Acts-Supp/99-2026/Published/20260610'
    '?DocDate=20260610">Interpretation (Amendment) Act 2026</a></td>'
    '<td class="col-no">Act 99 of 2026</td></tr>'
)

# zeeker.toml resource order (gazette first, per the PRD rolling-update
# sequence; act_versions after acts because it reads the extraction cache);
# acts is the fragments-enabled resource.
RESOURCE_ORDER = ("gazette_stream", "acts", "act_versions")


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _synth_detail(shorthand: str) -> str:
    """Minimal detail page for Acts without a committed fixture."""
    return f"""<html><body>
    <div class="global-vars"
         data-json='{{"lazyLoadFilter": null, "currViewFilter": {{"DocNo": "{shorthand}"}}}}'></div>
    <div class="legis-title">Current version as at 01 Jan 2026</div>
    <div class="front"><table><tr>
      <td class="longTitle">An Act relating to {shorthand}.</td>
    </tr></table></div>
    <div id="docTimeline"><ul>
      <li data-id="v1" class="selected">
        <a href="/Act/{shorthand}?ValidDate=20260101">01 Jan 2026</a>
      </li>
    </ul></div>
    <div class="body">
      <div class="prov1">
        <table><tr><td class="prov1Hdr" id="pr1-"><span class="">Short title</span></td></tr></table>
        <table><tr><td class="prov1Txt"><strong>1.</strong>
          This Act is the synthesized fixture for {shorthand}.</td></tr></table>
      </div>
    </div>
    </body></html>"""


def _load_resource(name: str) -> ModuleType:
    """Load a resource module exactly the way zeeker does (fresh instance)."""
    spec = importlib.util.spec_from_file_location(name, RESOURCES_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FixtureSite:
    """Mutable fixture-backed stand-in for sso.agc.gov.sg."""

    def __init__(self) -> None:
        self.pages: Dict[str, str] = {
            "listing_p1": _fixture("listing_p1.html"),
            "listing_p2": _fixture("listing_p2.html"),
            "acts_supp": _fixture("acts_supp_2026.html"),
            "sl_supp": _fixture("sl_supp_2026.html"),
        }
        self._ia1965 = _fixture("act_IA1965_raw.html")
        self._aa1928 = _fixture("act_AA1928_raw.html")
        self._lazyload = _fixture("lazyload_response.html")
        self.requests: list[str] = []  # every served path, in order
        self.transport = httpx.MockTransport(self._handle)

    def mutate_ia1965_stamp(self) -> None:
        page = self.pages["listing_p1"]
        assert page.count(IA1965_PDF_LINK) == 2  # mobile dropdown + desktop icon
        self.pages["listing_p1"] = page.replace(IA1965_PDF_LINK, IA1965_PDF_LINK_MUTATED)

    def add_acts_supp_item(self) -> None:
        page = self.pages["acts_supp"]
        assert "<tbody>" in page
        self.pages["acts_supp"] = page.replace("<tbody>", "<tbody>" + NEW_SUPP_ROW, 1)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)

        def page(body: str) -> httpx.Response:
            return httpx.Response(200, text=body, headers={"content-type": "text/html"})

        if path == "/Browse/Act/Current/All":
            return page(self.pages["listing_p1"])
        if path == "/Browse/Act/Current/All/1":
            return page(self.pages["listing_p2"])
        if path == "/Browse/Acts-Supp/Published/2026":
            return page(self.pages["acts_supp"])
        if path == "/Browse/SL-Supp/Published/2026":
            return page(self.pages["sl_supp"])
        if path == "/Details/GetLazyLoadContent":
            if request.url.params.get("SeriesId") == IA1965_PART2_SERIES:
                return page(self._lazyload)
            return page("<div></div>")  # series with no captured fixture
        match = re.fullmatch(r"/Act/([A-Za-z0-9.\-]+)", path)
        if match:
            shorthand = match.group(1)
            if shorthand == "IA1965":
                return page(self._ia1965)
            if shorthand == "AA1928":
                return page(self._aa1928)
            return page(_synth_detail(shorthand))
        return httpx.Response(404, text=f"no fixture route for {path}")


def _extraction_cache_state() -> list[tuple[str, int]]:
    """(name, mtime_ns) of every extraction-cache record — detects ANY write."""
    cache_dir = Path(".cache/statutes_extractions")
    return sorted((p.name, p.stat().st_mtime_ns) for p in cache_dir.glob("*.json"))


def run_build(db: sqlite_utils.Database, site: FixtureSite) -> None:
    """One ``zeeker build``: all resources in zeeker.toml order.

    Pins zeeker's double-``fetch_data`` mechanics for the fragments-enabled
    resource AND the fixed behaviour: the second (context) call must issue
    ZERO HTTP requests and extract nothing — the per-build marker makes it
    side-effect-free — while every real build's first call crawls the
    listing (a marker left over from a previous build never suppresses a
    run; the pipeline wipes ``.cache/`` between builds like ephemeral CI).
    """
    for name in RESOURCE_ORDER:
        module = _load_resource(name)
        module.TRANSPORT = site.transport
        existing = db[name] if db[name].exists() else None
        requests_before = len(site.requests)
        rows = module.fetch_data(existing)
        if name == "acts":
            listing_requests = [
                p for p in site.requests[requests_before:] if p.startswith("/Browse/Act/Current")
            ]
            assert listing_requests, "a real build must crawl the listing (marker must be stale)"
        if rows:
            db[name].insert_all(rows, replace=False)
        if name != "acts":
            continue
        # Fragments phase: zeeker reloads the module and calls fetch_data
        # again purely for fragments context (the result is never inserted).
        fragments_module = _load_resource(name)
        fragments_module.TRANSPORT = site.transport
        existing = db[name] if db[name].exists() else None
        requests_before = len(site.requests)
        cache_before = _extraction_cache_state()
        context = fragments_module.fetch_data(existing)
        assert (
            len(site.requests) == requests_before
        ), "second fetch_data call in the same build must issue ZERO HTTP requests"
        assert _extraction_cache_state() == cache_before, (
            "second fetch_data call must not extract anything "
            "(extraction cache must be untouched)"
        )
        assert context == []  # nothing to insert; fragments read the disk cache
        fragments_existing = db["acts_fragments"] if db["acts_fragments"].exists() else None
        fragment_rows = fragments_module.fetch_fragments_data(fragments_existing, context)
        if fragment_rows:
            db["acts_fragments"].insert_all(fragment_rows, replace=False)


def _clear_cache() -> None:
    """Simulate ephemeral CI: .cache/ does not survive between builds."""
    shutil.rmtree(".cache", ignore_errors=True)


def _counts(db: sqlite_utils.Database) -> Dict[str, int]:
    return {
        table: db[table].count
        for table in ("acts", "acts_fragments", "act_versions", "gazette_stream")
    }


def _acts_column(db: sqlite_utils.Database, column: str) -> Dict[str, object]:
    return {row[0]: row[1] for row in db.execute(f"SELECT shorthand, [{column}] FROM acts")}


def _load_sanity_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "sanity_checks", PROJECT_ROOT / "scripts" / "sanity_checks.py"
    )
    sanity = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sanity)
    return sanity


def _sanity_violations(db_path: Path) -> list[str]:
    """Run the sanity-check invariants against ``db_path``; return violations."""
    sanity = _load_sanity_module()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        violations: list[str] = []
        sanity.check_content_has_fragments(conn, violations)
        sanity.check_unique_indexes(conn, violations)
        return violations
    finally:
        conn.close()


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory):
    """Run the five-build scenario once; tests assert on the snapshots."""
    workdir = tmp_path_factory.mktemp("offline_build")
    mp = pytest.MonkeyPatch()
    mp.chdir(workdir)
    mp.setenv("STATUTES_IGNORE_WINDOW", "1")  # MockTransport only — no live traffic
    mp.setenv("STATUTES_NO_DELAY", "1")
    mp.setenv("STATUTES_EXTRACT_MAX_PER_RUN", "600")  # drain the whole queue in run 1
    mp.delenv("STATUTES_EXTRACT_ENABLED", raising=False)

    site = FixtureSite()
    db = sqlite_utils.Database(workdir / "statutes.db")
    snapshots = {}

    # Run 1 — clean checkout, full build. A SINGLE from-scratch build must
    # already satisfy the sanity-check invariants (unique-index tripwires
    # included): the S3 DB is the only durable state, so in production every
    # night is potentially a "first build".
    _clear_cache()
    run_build(db, site)
    snapshots["run1"] = SimpleNamespace(
        counts=_counts(db),
        extracted_at=_acts_column(db, "extracted_at"),
        gazette_series_counts={
            row[0]: row[1]
            for row in db.execute("SELECT series, COUNT(*) FROM gazette_stream GROUP BY series")
        },
        sanity_violations=_sanity_violations(workdir / "statutes.db"),
    )
    db["acts_fragments"].enable_fts(
        ["content_text", "heading"], create_triggers=True, tokenize="unicode61"
    )

    # Run 2 — unchanged fixtures: must be a no-op.
    _clear_cache()
    run_build(db, site)
    snapshots["run2"] = SimpleNamespace(
        counts=_counts(db), extracted_at=_acts_column(db, "extracted_at")
    )

    # Run 3 — IA1965's listing_stamp changes; extraction disabled so the
    # re-queue itself is observable.
    site.mutate_ia1965_stamp()
    mp.setenv("STATUTES_EXTRACT_ENABLED", "0")
    _clear_cache()
    run_build(db, site)
    snapshots["run3"] = SimpleNamespace(
        counts=_counts(db),
        null_content=[
            row[0] for row in db.execute("SELECT shorthand FROM acts WHERE content_text IS NULL")
        ],
        ia_stamp=db.execute("SELECT listing_stamp FROM acts WHERE shorthand = 'IA1965'").fetchone()[
            0
        ],
    )

    # Run 4 — extraction re-enabled: drains exactly the re-queued Act.
    mp.setenv("STATUTES_EXTRACT_ENABLED", "1")
    _clear_cache()
    run_build(db, site)
    cache_files = sorted(Path(".cache/statutes_extractions").glob("*.json"))
    snapshots["run4"] = SimpleNamespace(
        counts=_counts(db),
        extracted_at=_acts_column(db, "extracted_at"),
        null_content=[
            row[0] for row in db.execute("SELECT shorthand FROM acts WHERE content_text IS NULL")
        ],
        cache_records=[json.loads(p.read_text(encoding="utf-8")) for p in cache_files],
    )

    # Run 5 — one Acts-Supp item above the cursor, an "(Amendment)" title
    # whose principal (IA1965) exists in the catalog. gazette_stream runs
    # FIRST (PRD ordering), so the invalidated principal is re-extracted by
    # the SAME night's queue drain.
    site.add_acts_supp_item()
    _clear_cache()
    run_build(db, site)
    snapshots["run5"] = SimpleNamespace(
        counts=_counts(db),
        extracted_at=_acts_column(db, "extracted_at"),
        new_row=next(
            db.query("SELECT * FROM gazette_stream WHERE series = 'acts_supp' AND number_seq = 99")
        ),
        ia_content=db.execute(
            "SELECT content_text FROM acts WHERE shorthand = 'IA1965'"
        ).fetchone()[0],
        null_content=[
            row[0] for row in db.execute("SELECT shorthand FROM acts WHERE content_text IS NULL")
        ],
    )

    try:
        yield SimpleNamespace(db=db, snapshots=snapshots, workdir=workdir)
    finally:
        mp.undo()


# =============================================================================
# Run 1 — definition-of-done invariants for a clean full build
# =============================================================================
def test_full_build_catalog_size(pipeline):
    # PRD says "~525"; the committed fixtures carry the real count, 523.
    assert pipeline.snapshots["run1"].counts["acts"] == 523


def test_full_build_aa1928_extracted_with_fragments(pipeline):
    db = pipeline.db
    act = next(db.query("SELECT * FROM acts WHERE shorthand = 'AA1928'"))
    assert act["id"] == ids.act_id("AA1928")
    assert act["has_content"] == 1
    assert act["long_title"].startswith("An Act for the better apportionment")
    assert act["current_as_at"] is not None
    assert "accruing from day to day" in act["content_text"]
    fragments = list(
        db.query(
            "SELECT * FROM acts_fragments WHERE parent_id = :p ORDER BY seq",
            {"p": ids.act_id("AA1928")},
        )
    )
    assert act["fragment_count"] == len(fragments) == 8  # longtitle + ss 1-7
    assert [f["unit_kind"] for f in fragments] == ["longtitle"] + ["section"] * 7
    assert fragments[3]["provision_id"] == "AA1928/s3"


def test_full_build_fts_finds_ia1965_s9a(pipeline):
    # PRD: searching "purposive interpretation" returns IA1965 s 9A.
    hits = list(pipeline.db["acts_fragments"].search("purposive interpretation"))
    provision_ids = {hit["provision_id"] for hit in hits}
    assert "IA1965/pt2/s9A" in provision_ids
    s9a = next(h for h in hits if h["provision_id"] == "IA1965/pt2/s9A")
    assert s9a["parent_id"] == ids.act_id("IA1965")


def test_full_build_act_versions_populated_for_extracted_acts(pipeline):
    db = pipeline.db
    # Every extracted Act contributed timeline rows; the real fixtures carry
    # their full histories.
    assert pipeline.snapshots["run1"].counts["act_versions"] >= 523
    ia_versions = list(
        db.query(
            "SELECT * FROM act_versions WHERE act_id = :a ORDER BY valid_date",
            {"a": ids.act_id("IA1965")},
        )
    )
    assert len(ia_versions) > 10
    assert sum(v["is_current"] for v in ia_versions) == 1
    assert all(v["shorthand"] == "IA1965" for v in ia_versions)
    aa_versions = list(
        db.query("SELECT * FROM act_versions WHERE act_id = :a", {"a": ids.act_id("AA1928")})
    )
    assert aa_versions


def test_full_build_gazette_stream_rows_present(pipeline):
    counts = pipeline.snapshots["run1"].gazette_series_counts
    assert counts["sl_supp"] == 20  # the committed fixture pages
    assert counts["acts_supp"] == 15
    db = pipeline.db
    kinds = {row[0] for row in db.execute("SELECT DISTINCT event_kind FROM gazette_stream")}
    assert kinds <= {"new", "amendment", "revocation", "commencement", "corrigenda", "other"}


def test_single_from_scratch_build_passes_sanity_checks(pipeline):
    """One clean build must end with ALL invariants intact — including the
    four unique-index tripwires (production deploys after every single
    build, and the S3 DB being the only durable state means a from-scratch
    rebuild can happen any night)."""
    assert pipeline.snapshots["run1"].sanity_violations == []


# =============================================================================
# Run 2 — unchanged fixtures are a no-op
# =============================================================================
def test_second_run_is_a_noop(pipeline):
    run1, run2 = pipeline.snapshots["run1"], pipeline.snapshots["run2"]
    assert run2.counts == run1.counts
    assert run2.extracted_at == run1.extracted_at  # nothing re-extracted


# =============================================================================
# Run 3 — listing_stamp mutation re-queues exactly one Act
# =============================================================================
def test_stamp_mutation_requeues_exactly_one_act(pipeline):
    run3 = pipeline.snapshots["run3"]
    assert run3.null_content == ["IA1965"]
    assert run3.ia_stamp == "20260611031500"  # stored stamp followed the listing
    assert run3.counts == pipeline.snapshots["run2"].counts  # no rows added/lost


# =============================================================================
# Run 4 — the re-queued Act (and only it) is re-extracted
# =============================================================================
def test_requeued_act_is_reextracted(pipeline):
    run2, run4 = pipeline.snapshots["run2"], pipeline.snapshots["run4"]
    assert run4.null_content == []
    assert run4.counts == run2.counts  # wholesale fragment replacement, same ids
    changed = {
        shorthand
        for shorthand, stamp in run4.extracted_at.items()
        if stamp != run2.extracted_at[shorthand]
    }
    assert changed == {"IA1965"}


# =============================================================================
# Run 5 — stream item above the cursor + principal invalidation
# =============================================================================
def test_supp_item_above_cursor_adds_exactly_one_stream_row(pipeline):
    run4, run5 = pipeline.snapshots["run4"], pipeline.snapshots["run5"]
    assert run5.counts["gazette_stream"] == run4.counts["gazette_stream"] + 1
    row = run5.new_row
    assert row["id"] == ids.gazette_id("acts_supp", 2026, 99)
    assert row["number"] == "99 of 2026"
    assert row["title"] == "Interpretation (Amendment) Act 2026"
    assert row["event_kind"] == "amendment"
    assert row["published_date"] == "2026-06-10"


def test_amendment_invalidated_principal_reextracted_same_night(pipeline):
    """PRD ordering: stream ingestion runs BEFORE the queue drain, so the
    principal invalidated by tonight's amendment event is re-extracted in
    the same build — not left NULL for the next night."""
    run4, run5 = pipeline.snapshots["run4"], pipeline.snapshots["run5"]
    row = run5.new_row
    assert row["principal_title"] == "Interpretation Act"
    assert row["principal_resolved"] == 1
    assert row["principal_id"] == ids.act_id("IA1965")
    assert run5.ia_content is not None  # re-extracted the SAME night
    assert run5.null_content == []
    changed = {
        shorthand
        for shorthand, stamp in run5.extracted_at.items()
        if stamp != run4.extracted_at[shorthand]
    }
    assert changed == {"IA1965"}  # and ONLY the invalidated principal
    assert run5.counts["acts"] == run4.counts["acts"]
    assert run5.counts["acts_fragments"] == run4.counts["acts_fragments"]


# =============================================================================
# Sanity-check script invariants hold on the built database
# =============================================================================
def test_sanity_check_invariants_hold(pipeline):
    assert _sanity_violations(pipeline.workdir / "statutes.db") == []


# =============================================================================
# Per-build marker semantics (zeeker's second fetch_data call must be free)
# =============================================================================
@pytest.fixture()
def offline_env(tmp_path, monkeypatch):
    """Isolated cwd + fixture-only env for standalone resource-level tests."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STATUTES_IGNORE_WINDOW", "1")  # MockTransport only
    monkeypatch.setenv("STATUTES_NO_DELAY", "1")
    return tmp_path


def test_fresh_marker_skips_crawl_and_stale_marker_runs_normally(offline_env, monkeypatch):
    """The marker only suppresses the SAME build's second fetch_data call:
    fresh marker -> zero requests, no rows; aged marker (next nightly run,
    or any marker older than the freshness horizon) -> full crawl."""
    monkeypatch.setenv("STATUTES_EXTRACT_ENABLED", "0")  # discovery only
    site = FixtureSite()
    acts = _load_resource("acts")
    acts.TRANSPORT = site.transport

    # Fresh marker, as written by the first fetch_data of this build.
    acts.build_state.write_marker()
    assert acts.fetch_data(None) == []
    assert site.requests == []  # ZERO HTTP requests

    # Age the marker past the freshness horizon: must crawl again.
    aged = datetime.now(timezone.utc) - timedelta(hours=acts.build_state.MARKER_FRESH_HOURS + 1)
    acts.build_state.write_marker(aged)
    rows = acts.fetch_data(None)
    assert len(rows) == 523
    assert any(p.startswith("/Browse/Act/Current") for p in site.requests)
    # ... and the real run refreshed the marker for its own fragments phase.
    assert acts.build_state.marker_is_fresh()


def test_marker_freshness_predicate(offline_env):
    build_state = _load_resource("build_state")
    assert not build_state.marker_is_fresh()  # missing -> stale
    build_state.write_marker()
    assert build_state.marker_is_fresh()
    build_state.write_marker(datetime.now(timezone.utc) - timedelta(hours=7))
    assert not build_state.marker_is_fresh()  # aged -> stale
    build_state.write_marker(datetime.now(timezone.utc) + timedelta(hours=1))
    assert not build_state.marker_is_fresh()  # future-dated -> stale
    build_state.MARKER_PATH.write_text("not json", encoding="utf-8")
    assert not build_state.marker_is_fresh()  # corrupt -> stale (never suppress)


# =============================================================================
# gazette_stream exits cleanly on window close / open circuit breaker
# =============================================================================
def test_gazette_stream_window_close_mid_poll_keeps_ingested_rows(offline_env, monkeypatch):
    site = FixtureSite()
    gazette = _load_resource("gazette_stream")
    gazette.TRANSPORT = site.transport
    calls = {"n": 0}

    def closing_window() -> None:
        calls["n"] += 1
        if calls["n"] > 1:  # first request inside the window, then hard stop
            raise gazette.sso_window.WindowClosedError("06:45 SGT hard stop (test)")

    monkeypatch.setattr(gazette.sso_window, "require_window", closing_window)
    rows = gazette.fetch_data(None)
    assert rows, "rows ingested before the stop must be returned"
    assert {row["series"] for row in rows} == {gazette.parse_supp.SERIES[0]}


def test_gazette_stream_circuit_breaker_exits_cleanly(offline_env, monkeypatch):
    gazette = _load_resource("gazette_stream")

    def breaker_open(self, *args, **kwargs):
        raise gazette.sso_client.CircuitBreakerOpen("5 consecutive failures (test)")

    monkeypatch.setattr(gazette.sso_client.SSOClient, "get", breaker_open)
    assert gazette.fetch_data(None) == []


# =============================================================================
# Incomplete bodies are extraction FAILURES, never partial extractions
# =============================================================================
def _lazy_detail(fragments_json: str) -> str:
    """Minimal detail page with one lazy-load body series placeholder."""
    return f"""<html><body>
    <div class="global-vars"
         data-json='{{"lazyLoadFilter": {{"TocSysId": "toc-1"}},
                      "lazyLoadContentUrl": "/Details/GetLazyLoadContent",
                      "fragments": {fragments_json},
                      "currViewFilter": {{"DocNo": "XX2026"}}}}'></div>
    <div class="legis-title">Current version as at 01 Jan 2026</div>
    <div class="front"><table><tr><td class="longTitle">An Act.</td></tr></table></div>
    <div class="body"><div class="dms" data-field="seriesId" data-term="series-1"></div></div>
    </body></html>"""


def _drain_one_act(detail_html: str, lazyload_body: str) -> Dict[str, object]:
    """Run the extraction drain for one synthetic Act; return its row dict."""
    acts = _load_resource("acts")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Act/XX2026":
            return httpx.Response(200, text=detail_html)
        if request.url.path == "/Details/GetLazyLoadContent":
            return httpx.Response(200, text=lazyload_body)
        return httpx.Response(404, text="no route")

    new_row: Dict[str, object] = {"content_text": None, "has_content": False, "extracted_at": None}
    with acts.sso_client.SSOClient(transport=httpx.MockTransport(handler)) as client:
        acts._drain_queue(client, [("XX2026", "https://sso.agc.gov.sg/Act/XX2026", new_row)], None)
    return new_row


def test_series_missing_from_fragments_map_is_extraction_failure(offline_env):
    """A body series the fragments map cannot resolve = quarantine: the act
    stays unextracted (content_text NULL re-queues it) and no extraction
    cache record is written — never a silently partial body."""
    row = _drain_one_act(_lazy_detail("{}"), "<div>body</div>")
    assert row["content_text"] is None
    assert row["has_content"] is False
    assert row["extracted_at"] is None
    assert list(Path(".cache/statutes_extractions").glob("*.json")) == []


def test_empty_lazyload_payload_is_extraction_failure(offline_env):
    fragments = '{"series-1": {"Item1": "frag-1", "Item2": "20260101000000"}}'
    row = _drain_one_act(_lazy_detail(fragments), "")
    assert row["content_text"] is None
    assert row["has_content"] is False
    assert row["extracted_at"] is None
    assert list(Path(".cache/statutes_extractions").glob("*.json")) == []


def test_extraction_cache_records_have_the_pinned_shape(pipeline):
    """Cross-module contract: acts writes the cache act_versions reads."""
    records = pipeline.snapshots["run4"].cache_records
    assert len(records) == 1, "run 4 should have re-extracted exactly IA1965"
    record = records[0]
    assert record["shorthand"] == "IA1965"
    assert set(record) >= {"act_id", "shorthand", "extracted_at", "fragments", "versions"}
    assert record["act_id"] == ids.act_id("IA1965")
    fragment_keys = {
        "provision_id",
        "provision_num",
        "unit_kind",
        "heading",
        "part_num",
        "part_heading",
        "seq",
        "content_text",
        "structure_json",
        "source_notes",
    }
    assert all(fragment_keys <= set(f) for f in record["fragments"])
    version_keys = {
        "valid_date",
        "doc_date",
        "amended_by",
        "historical_url",
        "pdf_url",
        "is_current",
    }
    assert all(version_keys <= set(v) for v in record["versions"])
