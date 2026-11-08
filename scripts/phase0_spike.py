#!/usr/bin/env python3
"""Phase-0 spike runner: executes the PRD "Phase 0" checklist against SSO and
writes ``docs/phase0-findings.md``.

Checklist (each step gets a VERIFIED / FAILED / INCONCLUSIVE marker):

(a) robots.txt - record User-agent groups, Disallow rules, Crawl-delay.
(b) Lazy-load detection - fetch one short Act (AA1928) and one long Act
    (IA1965) raw and report whether body provisions are present in the
    initial HTML (heuristic: provision-ish nodes vs TOC entries), plus any
    lazy-load markers / endpoint references embedded in the page's own
    HTML/JS. We only *record* references found in the page - we never probe.
(c) Lazy-load replay - if ``raw_fixtures/lazyload_urls.txt`` exists (URLs
    captured manually from browser devtools, one per line), replay each with
    SSOClient and record status + content shape. URLs not on sso.agc.gov.sg
    are refused. NOTHING is guessed or probed beyond that file.
(d) ``listing_stamp`` HYPOTHESIS - extract the ``_`` cache-buster from ~5
    Acts' PDF links on the listing and cross-check against the latest
    version-timeline date / status banner on each detail page.
(e) ``?ViewType=Print`` - report whether the print variant carries the full
    body (provision count vs the detail page's TOC count).
(f) ``?ViewType=Rss`` - record the feed's XML shape.

ToU compliance: every live request goes through ``SSOClient``, which calls
the 03:00-07:00 SGT window guard before each request and hard-stops new
requests at 06:45 SGT. If the window closes mid-run, remaining steps are
marked INCONCLUSIVE, the findings file is still written, and the script
exits 0 with a "window closed, checkpointed" message.

This script's ONLY purpose is live fetching from sso.agc.gov.sg, so it
hard-refuses to start (exit 2) when ``STATUTES_IGNORE_WINDOW`` or
``STATUTES_NO_DELAY`` is present in the environment: those knobs exist for
fixture/offline work and would silently defeat the ToU window guard and the
robots.txt crawl-delay pacing if inherited from the caller's shell.

Run (inside the window):  uv run python scripts/phase0_spike.py
"""

from __future__ import annotations

import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import click
from bs4 import BeautifulSoup

# Import sibling resource modules by bare name (the way zeeker's file loader
# and sso_client itself do). Importing them as ``resources.sso_window`` would
# load a SECOND copy of the module whose WindowClosedError is a different
# class from the one SSOClient raises, so the except clauses below would
# silently fail to catch it.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "resources"))

from sso_client import (  # noqa: E402
    BASE_URL,
    CircuitBreakerOpen,
    SSOClient,
    cache_write,
)
from sso_window import WindowClosedError, now_sgt  # noqa: E402

FINDINGS_PATH = ROOT / "docs" / "phase0-findings.md"
LAZYLOAD_URLS_FILE = ROOT / "raw_fixtures" / "lazyload_urls.txt"
REPLAY_DIR = ROOT / "tests" / "fixtures"

LISTING_PATH = "/Browse/Act/Current/All"
LISTING_PARAMS = {"PageSize": "500", "SortBy": "Title", "SortOrder": "ASC"}

VERIFIED, FAILED, INCONCLUSIVE = "VERIFIED", "FAILED", "INCONCLUSIVE"

#: Fixture/offline-only env knobs that must never reach a live-fetching run.
FORBIDDEN_ENV_VARS = ("STATUTES_IGNORE_WINDOW", "STATUTES_NO_DELAY")


def refuse_fixture_env() -> None:
    """Exit 2 if fixture-only env vars are set (ToU window + pacing guard).

    This script live-fetches from sso.agc.gov.sg and nothing else; there is
    no legitimate fixture mode for it, so an inherited
    ``STATUTES_IGNORE_WINDOW`` / ``STATUTES_NO_DELAY`` can only mean the
    caller's shell is about to defeat the extraction-window guard or the
    robots.txt crawl-delay. Refuse loudly before any client is built.
    """
    present = [name for name in FORBIDDEN_ENV_VARS if name in os.environ]
    if present:
        click.echo(
            f"ERROR: {', '.join(present)} set in the environment. This script issues LIVE "
            "requests to sso.agc.gov.sg; the window guard and polite pacing must stay active "
            "(SSO Terms of Use cl 13(d)). Unset the variable(s) and re-run inside the "
            "03:00-07:00 SGT window.",
            err=True,
        )
        sys.exit(2)


# --- HTML heuristics --------------------------------------------------------

# SSO body provisions render in elements whose class carries a "prov" token
# (e.g. prov1, prov1Hdr, prov1Txt). Matched per-class by BeautifulSoup.
PROV_CLASS_RE = re.compile(r"prov\d", re.I)
TOC_ANCHOR_RE = re.compile(r"^#?pr\d", re.I)
TOC_CLASS_RE = re.compile(r"toc", re.I)
LAZY_HINT_RE = re.compile(r"lazy[\s_-]?load", re.I)
# Quoted strings in the page's own HTML/JS that look like lazy-load endpoints.
LAZY_URL_RE = re.compile(r"""["']([^"'<>\s]*[Ll]azy[Ll]oad[^"'<>\s]*)["']""")
PDF_STAMP_RE = re.compile(r"/Act/([A-Za-z0-9.-]+)\?ViewType=Pdf&(?:amp;)?_=(\d{8,14})")
HISTORICAL_DATE_RES = (
    re.compile(r"ValidDate=(\d{8})"),
    re.compile(r"/Historical/(\d{8})"),
)
BANNER_RE = re.compile(r"Current version as at\s+(\d{1,2}\s+\w+\s+\d{4})")


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def count_provision_nodes(soup: BeautifulSoup) -> int:
    return len(soup.find_all(class_=PROV_CLASS_RE))


def count_toc_entries(soup: BeautifulSoup) -> int:
    anchors = [a for a in soup.find_all("a", href=True) if TOC_ANCHOR_RE.match(a["href"])]
    if anchors:
        return len(anchors)
    return len(soup.find_all(class_=TOC_CLASS_RE))


def parse_human_date(text: str) -> Optional[str]:
    """'5 Dec 2025' / '5 December 2025' -> 'YYYYMMDD', else None."""
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    return None


def timeline_dates(soup: BeautifulSoup) -> list[str]:
    """All YYYYMMDD valid-dates found in /Historical/ links, sorted ascending."""
    dates: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/Historical/" not in href:
            continue
        for rx in HISTORICAL_DATE_RES:
            m = rx.search(href)
            if m:
                dates.add(m.group(1))
                break
    return sorted(dates)


def analyze_act_html(html: str) -> dict:
    """Heuristic body-vs-TOC analysis of one Act detail page's raw HTML."""
    soup = soup_of(html)
    toc_count = count_toc_entries(soup)
    prov_count = count_provision_nodes(soup)
    text_len = len(soup.get_text(" ", strip=True))
    lazy_hits = len(LAZY_HINT_RE.findall(html))
    candidate_urls = sorted(set(LAZY_URL_RE.findall(html)))
    body_present: Optional[bool] = None
    if toc_count > 0:
        body_present = prov_count >= max(1, int(toc_count * 0.8))
    return {
        "html_kb": len(html) // 1024,
        "text_len": text_len,
        "toc_count": toc_count,
        "prov_count": prov_count,
        "lazy_hits": lazy_hits,
        "candidate_urls": candidate_urls,
        "body_present": body_present,
    }


# --- step plumbing ----------------------------------------------------------


class Step:
    """One checklist item: a status marker plus findings lines."""

    def __init__(self, key: str, title: str) -> None:
        self.key = key
        self.title = title
        self.status = INCONCLUSIVE
        self.lines: list[str] = []

    def log(self, msg: str) -> None:
        self.lines.append(msg)
        click.echo(f"    {msg}")


class SpikeRun:
    """Shared client + page cache + window/breaker state across steps."""

    def __init__(self, client: SSOClient) -> None:
        self.client = client
        self.window_closed = False
        self.breaker_open = False
        self.pages: dict[str, str] = {}  # cache key -> body text

    def get_text(self, url_or_path: str, params: Optional[dict] = None, *, key: str = "") -> str:
        """Fetch (with per-run cache). Raises WindowClosedError upward."""
        cache_key = key or url_or_path
        if cache_key in self.pages:
            return self.pages[cache_key]
        click.echo(f"    GET {url_or_path} params={params or {}}")
        try:
            resp = self.client.get(url_or_path, params=params)
        except WindowClosedError:
            self.window_closed = True
            raise
        except CircuitBreakerOpen:
            self.breaker_open = True
            raise
        resp.raise_for_status()
        self.pages[cache_key] = resp.text
        return resp.text

    def detail_page(self, shorthand: str) -> str:
        return self.get_text(f"/Act/{shorthand}", key=f"detail:{shorthand}")

    def run_step(self, step: Step, fn) -> None:
        """Run one step; one step's failure never kills the run."""
        click.echo(f"\n[{step.key}] {step.title}")
        if self.window_closed:
            step.status = INCONCLUSIVE
            step.log("Not run: extraction window closed before this step.")
            return
        if self.breaker_open:
            step.status = INCONCLUSIVE
            step.log("Not run: circuit breaker open (consecutive failures).")
            return
        try:
            fn(step)
        except WindowClosedError:
            step.status = INCONCLUSIVE
            step.log("Window closed mid-step; partial results above (if any).")
        except CircuitBreakerOpen as exc:
            step.status = FAILED
            step.log(f"Circuit breaker open: {exc}")
        except Exception as exc:  # noqa: BLE001 - keep the checklist moving
            step.status = FAILED
            step.log(f"Unexpected error: {exc!r}")
        click.echo(f"    -> {step.status}")


# --- checklist steps --------------------------------------------------------


def step_robots(run: SpikeRun, step: Step) -> None:
    body = run.get_text("/robots.txt")
    step.log(f"Fetched {BASE_URL}/robots.txt ({len(body)} bytes).")
    agent = None
    disallows: list[tuple[str, str]] = []
    crawl_delays: list[tuple[str, str]] = []
    for raw_line in body.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            agent = value
        elif field == "disallow" and value:
            disallows.append((agent or "*", value))
        elif field == "crawl-delay":
            crawl_delays.append((agent or "*", value))
    if disallows:
        for agent_name, path in disallows:
            step.log(f"Disallow [{agent_name}]: {path}")
    else:
        step.log("No Disallow rules found.")
    if crawl_delays:
        for agent_name, delay in crawl_delays:
            step.log(f"Crawl-delay [{agent_name}]: {delay}s")
    else:
        step.log("No Crawl-delay directive found.")
    blocked = [p for _, p in disallows if p.startswith(("/Act", "/Browse", "/"))]
    relevant = [p for p in blocked if p == "/" or p.startswith(("/Act", "/Browse"))]
    if relevant:
        step.log(f"ATTENTION: Disallow rules touch our routes: {relevant} - those routes are OUT.")
    step.status = VERIFIED


def step_lazyload_detection(run: SpikeRun, step: Step, short_act: str, long_act: str) -> None:
    any_failure = False
    for label, shorthand in (("short", short_act), ("long", long_act)):
        try:
            html = run.detail_page(shorthand)
        except WindowClosedError:
            raise
        except Exception as exc:  # noqa: BLE001
            any_failure = True
            step.log(f"{shorthand} ({label}): fetch failed: {exc}")
            continue
        info = analyze_act_html(html)
        setattr(run, f"analysis_{shorthand}", info)
        step.log(
            f"{shorthand} ({label}): html={info['html_kb']} KB, text={info['text_len']} chars, "
            f"toc_entries={info['toc_count']}, provision_nodes={info['prov_count']}, "
            f"lazy_markers={info['lazy_hits']}"
        )
        if info["body_present"] is True:
            step.log(f"{shorthand}: body provisions PRESENT in initial HTML.")
        elif info["body_present"] is False:
            step.log(f"{shorthand}: body provisions NOT in initial HTML -> lazy-loaded.")
        else:
            step.log(f"{shorthand}: TOC heuristic found nothing; eyeball the fixture manually.")
        for url in info["candidate_urls"][:10]:
            step.log(f"{shorthand}: page references lazy-load-ish endpoint: {url}")
    step.status = FAILED if any_failure else VERIFIED


def step_lazyload_replay(run: SpikeRun, step: Step, urls_file: Path, replay_dir: Path) -> None:
    if not urls_file.exists():
        step.status = INCONCLUSIVE
        step.log(f"{urls_file} not found - capture lazy-load XHR URLs manually in browser")
        step.log("devtools (manual browsing is permitted anytime under the ToU), one URL per")
        step.log("line, then re-run this script inside the window. NO endpoints were probed.")
        return
    urls = [
        line.strip()
        for line in urls_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not urls:
        step.status = INCONCLUSIVE
        step.log(f"{urls_file} exists but contains no URLs.")
        return
    sso_host = urlparse(BASE_URL).netloc.lower()
    successes = 0
    for i, url in enumerate(urls, start=1):
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc.lower() != sso_host:
            step.log(f"[{i}] REFUSED (host {parsed.netloc!r} is not {sso_host}): {url}")
            continue
        try:
            resp = run.client.get(url)
        except WindowClosedError:
            run.window_closed = True
            step.log(f"[{i}] window closed before this replay.")
            raise
        except CircuitBreakerOpen:
            run.breaker_open = True
            raise
        except Exception as exc:  # noqa: BLE001
            step.log(f"[{i}] {url} -> ERROR {exc}")
            continue
        ctype = resp.headers.get("content-type", "?")
        body = resp.text
        shape = analyze_act_html(body) if "html" in ctype else None
        out_path = replay_dir / f"lazyload_replay_{i:02d}.html"
        cache_write(out_path, body)
        msg = f"[{i}] {url} -> HTTP {resp.status_code}, {ctype}, {len(body)} chars"
        if shape:
            msg += f", provision_nodes={shape['prov_count']}"
        step.log(msg)
        try:
            saved_as = str(out_path.relative_to(ROOT))
        except ValueError:  # replay dir outside the repo
            saved_as = str(out_path)
        step.log(f"[{i}] body saved to {saved_as}")
        if resp.status_code == 200 and len(body) > 200:
            successes += 1
    if successes:
        step.status = VERIFIED
        step.log(f"{successes}/{len(urls)} replays returned plausible content - route 1 viable.")
    else:
        step.status = FAILED
        step.log("No replay returned usable content - route 1 (lazy-load XHR) looks dead.")


def step_listing_stamp(
    run: SpikeRun, step: Step, sample_size: int, preferred: tuple[str, ...]
) -> None:
    listing_html = run.get_text(LISTING_PATH, params=LISTING_PARAMS, key="listing:p1")
    stamps: dict[str, str] = {}
    for m in PDF_STAMP_RE.finditer(listing_html):
        stamps.setdefault(m.group(1), m.group(2))
    step.log(f"Listing page 1: found `_` stamps for {len(stamps)} Acts.")
    if not stamps:
        step.status = FAILED
        step.log("No ViewType=Pdf&_= links found on the listing - hypothesis untestable here.")
        return
    # Prefer Acts whose detail pages this run already fetched (no extra requests),
    # then top up in listing order.
    sample = [sh for sh in preferred if sh in stamps]
    for sh in stamps:
        if len(sample) >= sample_size:
            break
        if sh not in sample:
            sample.append(sh)

    rows: list[tuple[str, str, str, str, str, str]] = []
    exact = 0
    checked = 0
    for sh in sample[:sample_size]:
        stamp = stamps[sh]
        stamp_date = stamp[:8]
        try:
            detail = run.detail_page(sh)
        except WindowClosedError:
            step.log(f"{sh}: window closed before its detail fetch; stopping the sample here.")
            break
        except Exception as exc:  # noqa: BLE001
            rows.append((sh, stamp, stamp_date, "?", "?", f"fetch failed: {exc}"))
            continue
        soup = soup_of(detail)
        tl = timeline_dates(soup)
        latest_valid = tl[-1] if tl else "?"
        banner_m = BANNER_RE.search(detail)
        banner = parse_human_date(banner_m.group(1)) if banner_m else None
        banner_str = banner or "?"
        checked += 1
        if stamp_date in (latest_valid, banner_str):
            exact += 1
            verdict = "MATCH"
        elif latest_valid != "?" and stamp_date >= latest_valid:
            verdict = "stamp later than timeline (plausible: consolidation upload date)"
        else:
            verdict = "MISMATCH"
        rows.append((sh, stamp, stamp_date, latest_valid, banner_str, verdict))
    for sh, stamp, stamp_date, latest_valid, banner_str, verdict in rows:
        step.log(
            f"{sh}: _={stamp} (date {stamp_date}) | latest timeline ValidDate={latest_valid} "
            f"| banner current-as-at={banner_str} -> {verdict}"
        )
    if checked == 0:
        step.status = INCONCLUSIVE
        step.log("No Act could be cross-checked.")
    elif exact == checked:
        step.status = VERIFIED
        step.log(f"HYPOTHESIS VERIFIED on {exact}/{checked} sampled Acts: `_` is a ")
        step.log("last-modified timestamp usable as `listing_stamp` for change detection.")
    elif exact > 0:
        step.status = INCONCLUSIVE
        step.log(f"PARTIAL: {exact}/{checked} exact matches. Stamp may be upload time rather")
        step.log("than validity date - still usable for *change detection* (diff, not parse),")
        step.log("but re-check on a second night before relying on it.")
    else:
        step.status = FAILED
        step.log(f"HYPOTHESIS FAILED: 0/{checked} matches. Fall back to per-Act RSS feeds for")
        step.log("change detection (PRD Phase 3 fallback).")


def step_print_view(run: SpikeRun, step: Step, act: str) -> None:
    print_html = run.get_text(f"/Act/{act}", params={"ViewType": "Print"}, key=f"print:{act}")
    print_info = analyze_act_html(print_html)
    step.log(
        f"{act}?ViewType=Print: html={print_info['html_kb']} KB, "
        f"provision_nodes={print_info['prov_count']}, text={print_info['text_len']} chars"
    )
    toc_count = None
    try:
        detail = run.detail_page(act)
        toc_count = count_toc_entries(soup_of(detail))
        step.log(f"{act} detail page TOC entries: {toc_count}")
    except WindowClosedError:
        step.log("Window closed before the TOC cross-check fetch; comparing on size only.")
    except Exception as exc:  # noqa: BLE001
        step.log(f"TOC cross-check fetch failed: {exc}")
    if toc_count:
        full = print_info["prov_count"] >= max(1, int(toc_count * 0.8))
        step.status = VERIFIED
        if full:
            step.log("Print view CONTAINS the full body -> route 2 (?ViewType=Print) viable.")
        else:
            step.log("Print view does NOT contain the full body (provision count << TOC count).")
    else:
        step.status = INCONCLUSIVE
        step.log("Could not establish a TOC baseline; eyeball act_*_print.html manually.")


def step_rss(run: SpikeRun, step: Step, act: str) -> None:
    body = run.get_text(f"/Act/{act}", params={"ViewType": "Rss"}, key=f"rss:{act}")
    try:
        root = ET.fromstring(body.encode("utf-8") if isinstance(body, str) else body)
    except ET.ParseError as exc:
        step.status = FAILED
        step.log(f"Feed did not parse as XML: {exc}; first 200 chars: {body[:200]!r}")
        return
    step.log(f"Root element: <{root.tag}> attrs={dict(root.attrib)}")
    channel = root.find("channel")
    items = channel.findall("item") if channel is not None else root.findall(".//item")
    if channel is not None:
        title = channel.findtext("title", default="?")
        step.log(f"Channel title: {title!r}")
    step.log(f"Item count: {len(items)}")
    tags: set[str] = set()
    for item in items[:5]:
        tags.update(child.tag for child in item)
    if tags:
        step.log(f"Item child tags (first 5 items): {sorted(tags)}")
    if items:
        first = items[0]
        step.log(
            "First item: title="
            f"{(first.findtext('title') or '?').strip()!r}, "
            f"pubDate={(first.findtext('pubDate') or '?').strip()!r}, "
            f"link={(first.findtext('link') or '?').strip()!r}"
        )
    step.status = VERIFIED


# --- findings rendering -----------------------------------------------------


def route_summary(
    steps: dict[str, Step], run: SpikeRun, short_act: str, long_act: str
) -> list[str]:
    lines: list[str] = []
    long_info = getattr(run, f"analysis_{long_act}", None)
    short_info = getattr(run, f"analysis_{short_act}", None)
    if long_info and long_info.get("body_present") is True:
        lines.append(
            f"- Bodies appear PRESENT in initial HTML even for the long Act ({long_act}); "
            "the plain detail fetch may suffice - confirm against the rendered page."
        )
    if steps["c"].status == VERIFIED:
        lines.append("- Route 1 (lazy-load XHR replay) VERIFIED - preferred per the PRD.")
    if steps["e"].status == VERIFIED and any(
        "CONTAINS the full body" in line for line in steps["e"].lines
    ):
        lines.append("- Route 2 (?ViewType=Print) carries the full body - viable.")
    if steps["c"].status == FAILED and steps["e"].status != VERIFIED:
        lines.append(
            "- STOP AND SURFACE: routes 1 and 2 both unproven/failed. Per the PRD, the "
            "Docling/PDF fallback (route 3) changes the plan - do not proceed silently."
        )
    if not lines:
        lines.append(
            "- No route confirmed yet (likely missing raw_fixtures/lazyload_urls.txt or the "
            "window closed). Re-run next window."
        )
    if short_info and short_info.get("candidate_urls"):
        lines.append(
            "- Endpoint references found in page HTML/JS are recorded in section (b); only "
            "URLs captured in raw_fixtures/lazyload_urls.txt are ever replayed."
        )
    return lines


def render_findings(steps: dict[str, Step], run: SpikeRun, short_act: str, long_act: str) -> str:
    out: list[str] = []
    out.append("# Phase 0 — Spike findings")
    out.append("")
    out.append(f"Generated by `scripts/phase0_spike.py` at {now_sgt().isoformat()} (SGT).")
    if run.window_closed:
        out.append("")
        out.append(
            "**NOTE:** the 03:00–07:00 SGT extraction window closed mid-run; steps marked "
            "INCONCLUSIVE below were cut short. Re-run in the next window to fill them in."
        )
    out.append("")
    out.append("Markers: VERIFIED (checked against the live site), FAILED (checked and the")
    out.append("hypothesis/route is dead), INCONCLUSIVE (could not be determined this run).")
    out.append("")
    titles = {
        "a": "robots.txt",
        "b": "Lazy-load detection (initial HTML vs TOC)",
        "c": "Lazy-load endpoint replay (raw_fixtures/lazyload_urls.txt)",
        "d": "`listing_stamp` hypothesis (the `_` PDF-link cache-buster)",
        "e": "?ViewType=Print full-body check",
        "f": "?ViewType=Rss feed shape",
    }
    for key in "abcdef":
        step = steps[key]
        out.append(f"## ({key}) {titles[key]} — {step.status}")
        out.append("")
        for line in step.lines:
            out.append(f"- {line}")
        if not step.lines:
            out.append("- (no output)")
        out.append("")
    out.append("## Route decision summary")
    out.append("")
    out.extend(route_summary(steps, run, short_act, long_act))
    out.append("")
    return "\n".join(out)


# --- CLI ---------------------------------------------------------------------


@click.command()
@click.option("--short-act", default="AA1928", show_default=True, help="Short Act for step (b).")
@click.option(
    "--long-act",
    default="IA1965",
    show_default=True,
    help="Long Act for steps (b), (e) and (f).",
)
@click.option(
    "--stamp-sample",
    default=5,
    show_default=True,
    type=int,
    help="How many Acts to cross-check for the listing_stamp hypothesis.",
)
@click.option(
    "--lazyload-file",
    default=str(LAZYLOAD_URLS_FILE),
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Manually captured lazy-load XHR URLs, one per line.",
)
@click.option(
    "--findings",
    default=str(FINDINGS_PATH),
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Where to write the findings markdown.",
)
@click.option(
    "--replay-dir",
    default=str(REPLAY_DIR),
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Where to save lazy-load replay bodies.",
)
def main(
    short_act: str,
    long_act: str,
    stamp_sample: int,
    lazyload_file: Path,
    findings: Path,
    replay_dir: Path,
) -> None:
    """Run the PRD Phase-0 checklist against SSO and write docs/phase0-findings.md."""
    refuse_fixture_env()
    click.echo(f"Phase-0 spike against {BASE_URL} at {now_sgt().isoformat()} SGT")
    steps = {
        "a": Step("a", "robots.txt"),
        "b": Step("b", "lazy-load detection"),
        "c": Step("c", "lazy-load replay"),
        "d": Step("d", "listing_stamp hypothesis"),
        "e": Step("e", "?ViewType=Print"),
        "f": Step("f", "?ViewType=Rss"),
    }
    client = SSOClient()
    run = SpikeRun(client)
    try:
        run.run_step(steps["a"], lambda s: step_robots(run, s))
        run.run_step(steps["b"], lambda s: step_lazyload_detection(run, s, short_act, long_act))
        run.run_step(steps["c"], lambda s: step_lazyload_replay(run, s, lazyload_file, replay_dir))
        run.run_step(
            steps["d"],
            lambda s: step_listing_stamp(run, s, stamp_sample, (short_act, long_act)),
        )
        run.run_step(steps["e"], lambda s: step_print_view(run, s, long_act))
        run.run_step(steps["f"], lambda s: step_rss(run, s, long_act))
    finally:
        client.close()

    text = render_findings(steps, run, short_act, long_act)
    cache_write(findings, text)
    click.echo(f"\nFindings written to {findings}")
    click.echo("Status: " + ", ".join(f"({k}) {steps[k].status}" for k in "abcdef"))

    if run.window_closed:
        click.echo("window closed, checkpointed - findings saved with partial results; exiting 0")
        sys.exit(0)
    if any(steps[k].status == FAILED for k in "abcdef"):
        click.echo("At least one checklist step FAILED - read the findings before building on it.")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
