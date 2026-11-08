#!/usr/bin/env python3
"""Phase-0 fixture fetcher: save SSO pages under ``tests/fixtures/``.

Fetches (all inside the 03:00-07:00 SGT extraction window, enforced by
``resources.sso_window`` via ``SSOClient``):

- Acts listing pages 1 and 2 (``PageSize=500&SortBy=Title&SortOrder=ASC``).
  The page-2 URL is taken from page 1's own pager links when findable;
  otherwise it falls back to the ``/Browse/Act/Current/All/1`` path-segment
  guess. If ``listing_p2.html`` turns out byte-identical to
  ``listing_p1.html``, the fallback guess was wrong - check the pager HTML.
- Act detail pages for a configurable list. Defaults cover the parser's
  structural edge cases: AA1928 (short), IA1965 (long + fat definitions),
  EA1893 (illustrations / deep nesting), CoA1967 (substantial Schedules).
- The ``?ViewType=Print`` variant and the ``?ViewType=Rss`` amendments feed
  for one Act (default IA1965).
- ``robots.txt``.
- The 2026 Acts-Supp and SL-Supp "AS PUBLISHED" stream pages (page 1).

Each fetch is independently wrapped: one failure never kills the run, and a
summary table prints at the end. When the extraction window closes mid-run
the script stops issuing requests, keeps everything already saved (writes
are atomic via ``cache_write``), and exits 0 with a clear message.

Run (inside the window):  uv run python scripts/fetch_fixtures.py
Re-runs skip files that already exist; use ``--force`` to refetch.

This script's ONLY purpose is live fetching from sso.agc.gov.sg, so it
hard-refuses to start (exit 2) when ``STATUTES_IGNORE_WINDOW`` or
``STATUTES_NO_DELAY`` is present in the environment: those knobs exist for
fixture/offline work and would silently defeat the ToU window guard and the
robots.txt crawl-delay pacing if inherited from the caller's shell.

Exit codes: 0 = all fetched/skipped, or window closed cleanly;
1 = at least one non-window failure (circuit breaker, HTTP error, ...);
2 = refused to start (fixture-only env vars set).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

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
from sso_window import WindowClosedError  # noqa: E402

FIXTURES_DIR = ROOT / "tests" / "fixtures"

LISTING_PATH = "/Browse/Act/Current/All"
LISTING_PARAMS = {"PageSize": "500", "SortBy": "Title", "SortOrder": "ASC"}

# Structural edge cases the provision parser must survive (see PRD Phase 0).
DEFAULT_ACTS = ("AA1928", "IA1965", "EA1893", "CoA1967")

# Statuses for the summary table.
OK, SKIP, FAIL, WINDOW = "OK", "SKIP", "FAIL", "WINDOW"

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


def find_page2_href(listing_html: str) -> Optional[str]:
    """Extract the page-2 URL from the listing's own pager, if present.

    Only follows links the page itself exposes (ToU cl 13(c)/(d): never
    guess endpoints when the site offers them openly).
    """
    soup = BeautifulSoup(listing_html, "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/Browse/Act/Current" not in href:
            continue
        text = a.get_text(strip=True)
        aria = (a.get("aria-label") or "").strip().lower()
        if text == "2" or aria in ("next", "next page", "page 2"):
            return href
    return None


class FixtureRun:
    """Tracks per-target outcomes and the window/breaker stop flags."""

    def __init__(self, client: SSOClient, out_dir: Path, force: bool) -> None:
        self.client = client
        self.out_dir = out_dir
        self.force = force
        self.window_closed = False
        self.breaker_open = False
        self.results: list[tuple[str, str, int, str]] = []

    def fetch(
        self, filename: str, url_or_path: str, params: Optional[dict] = None
    ) -> Optional[str]:
        """Fetch one target; record outcome; return the body text or None."""
        out_path = self.out_dir / filename
        if out_path.exists() and not self.force:
            size = out_path.stat().st_size
            self.results.append((filename, SKIP, size, "already present"))
            click.echo(f"  skip  {filename} (already present, {size // 1024} KB)")
            return out_path.read_text(encoding="utf-8")
        if self.window_closed:
            self.results.append((filename, WINDOW, 0, "not attempted (window closed)"))
            return None
        if self.breaker_open:
            self.results.append((filename, FAIL, 0, "not attempted (circuit breaker open)"))
            return None
        click.echo(f"  GET   {url_or_path} params={params or {}}")
        try:
            resp = self.client.get(url_or_path, params=params)
            resp.raise_for_status()
        except WindowClosedError:
            self.window_closed = True
            self.results.append((filename, WINDOW, 0, "extraction window closed"))
            click.echo("  window closed - no new requests; finishing with what we have")
            return None
        except CircuitBreakerOpen as exc:
            self.breaker_open = True
            self.results.append((filename, FAIL, 0, f"circuit breaker open: {exc}"))
            click.echo(f"  FAIL  circuit breaker open: {exc}")
            return None
        except Exception as exc:  # noqa: BLE001 - one failure must not kill the run
            self.results.append((filename, FAIL, 0, str(exc)[:160]))
            click.echo(f"  FAIL  {exc}")
            return None
        cache_write(out_path, resp.text)
        size = len(resp.content)
        self.results.append((filename, OK, size, ""))
        click.echo(f"  saved {filename} ({size // 1024} KB)")
        return resp.text


@click.command()
@click.option(
    "--act",
    "acts",
    multiple=True,
    default=DEFAULT_ACTS,
    show_default=True,
    help="Act shorthand(s) whose detail pages to fetch.",
)
@click.option(
    "--print-act",
    default="IA1965",
    show_default=True,
    help="Act to fetch with ?ViewType=Print.",
)
@click.option(
    "--rss-act",
    default="IA1965",
    show_default=True,
    help="Act whose ?ViewType=Rss amendments feed to fetch.",
)
@click.option(
    "--supp-year",
    default=2026,
    show_default=True,
    type=int,
    help="Year for the Acts-Supp / SL-Supp published-stream pages.",
)
@click.option(
    "--out-dir",
    default=str(FIXTURES_DIR),
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory to save fixtures under.",
)
@click.option("--force", is_flag=True, help="Refetch even if the fixture file already exists.")
def main(
    acts: tuple[str, ...],
    print_act: str,
    rss_act: str,
    supp_year: int,
    out_dir: Path,
    force: bool,
) -> None:
    """Fetch Phase-0 HTML/XML fixtures from SSO (window-guarded, atomic writes)."""
    refuse_fixture_env()
    click.echo(f"Fixture fetch -> {out_dir} (base {BASE_URL})")
    client = SSOClient()
    run = FixtureRun(client, out_dir, force)
    try:
        # robots.txt first: cheapest request, and a compliance input for everything else.
        run.fetch("robots.txt", "/robots.txt")

        # Listing pages 1 + 2 (525 current Acts at PageSize=500 = 2 pages).
        listing_p1 = run.fetch("listing_p1.html", LISTING_PATH, params=LISTING_PARAMS)
        page2: Optional[str] = None
        if listing_p1:
            page2 = find_page2_href(listing_p1)
        if page2:
            click.echo(f"  pager link for page 2: {page2}")
            run.fetch("listing_p2.html", page2)
        else:
            click.echo("  page-2 pager link not found; falling back to /All/1 path segment")
            run.fetch("listing_p2.html", f"{LISTING_PATH}/1", params=LISTING_PARAMS)

        # Act detail pages (raw initial HTML - the lazy-load evidence).
        for shorthand in acts:
            run.fetch(f"act_{shorthand}_raw.html", f"/Act/{shorthand}")

        # Print variant and RSS feed.
        run.fetch(f"act_{print_act}_print.html", f"/Act/{print_act}", params={"ViewType": "Print"})
        run.fetch(f"rss_{rss_act}.xml", f"/Act/{rss_act}", params={"ViewType": "Rss"})

        # AS PUBLISHED supplement streams, page 1.
        run.fetch(f"acts_supp_{supp_year}.html", f"/Browse/Acts-Supp/Published/{supp_year}")
        run.fetch(f"sl_supp_{supp_year}.html", f"/Browse/SL-Supp/Published/{supp_year}")
    finally:
        client.close()

    click.echo("\nSummary:")
    click.echo(f"  {'fixture':<30} {'status':<8} {'bytes':>10}  note")
    for name, status, size, note in run.results:
        click.echo(f"  {name:<30} {status:<8} {size:>10}  {note}")

    failures = [r for r in run.results if r[1] == FAIL]
    fetched = sum(1 for r in run.results if r[1] == OK)
    skipped = sum(1 for r in run.results if r[1] == SKIP)
    click.echo(f"\n{fetched} fetched, {skipped} skipped, {len(failures)} failed.")

    if run.window_closed:
        click.echo("window closed, checkpointed - everything fetched so far is saved; exiting 0")
        sys.exit(0)
    if failures:
        click.echo(
            "Some fetches failed - see summary above. Re-run to retry (existing files skip)."
        )
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
