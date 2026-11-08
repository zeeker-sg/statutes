"""One-shot diagnostic: is the SSO PDF endpoint reachable, or WAF-blocked like the rest?

Background: on 2026-06-12 every scripted request to sso.agc.gov.sg returned 403
(including robots.txt) — a client-fingerprint block (Imperva/Incapsula), not a
path-level one. This checks ONE hypothesis: that the PDF endpoint
(``/Act/{SHORTHAND}?ViewType=Pdf``) might be served by separate infrastructure
(a CDN/file host) NOT behind the same WAF. Low prior, cheap to test.

It fetches, in order, via the polite window-guarded SSOClient (honest ZeekerBot
UA, no impersonation, no bypass — if it's blocked, it's blocked):
  1. the IA1965 PDF  (?ViewType=Pdf)         — the thing we care about
  2. the IA1965 detail HTML (control)        — known-blocked last night

For each it records: HTTP status, Content-Type, body size, whether the body is a
real PDF (``%PDF`` magic) or an Incapsula/Imperva block page (markers in body),
and saves the body under raw_fixtures/ for inspection. Verdict logic:
  - PDF 200 + %PDF magic, HTML 403  -> PDF on separate infra: PURSUE PDF route
  - both 403 / both block pages      -> confirmed client-level block: PDF is moot
  - both 200                         -> last night was transient; re-evaluate

ToU: live-only, window-guarded, refuses fixture env vars. Run inside 03:00-07:00 SGT.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "resources"))

import sso_client  # noqa: E402
import sso_window  # noqa: E402

FORBIDDEN_ENV_VARS = ("STATUTES_IGNORE_WINDOW", "STATUTES_NO_DELAY")
OUT_DIR = ROOT / "raw_fixtures"
BLOCK_MARKERS = ("incapsula", "imperva", "_Incapsula_Resource", "incident", "request unsuccessful")


def refuse_fixture_env() -> None:
    present = [name for name in FORBIDDEN_ENV_VARS if name in os.environ]
    if present:
        click.echo(
            f"ERROR: {', '.join(present)} set. This script issues LIVE requests to "
            "sso.agc.gov.sg; the window guard and pacing must stay active (SSO ToU cl 13(d)). "
            "Unset and re-run inside 03:00-07:00 SGT.",
            err=True,
        )
        sys.exit(2)


def _classify_body(body: bytes) -> str:
    if body[:5] == b"%PDF-":
        return "REAL PDF (%PDF magic)"
    lowered = body[:4000].lower()
    if any(m.encode() in lowered for m in BLOCK_MARKERS):
        return "WAF BLOCK PAGE (Incapsula/Imperva markers)"
    if body[:15].lstrip().lower().startswith((b"<!doctype", b"<html")):
        return "HTML (not a PDF — likely block/error page)"
    return "UNKNOWN (neither PDF magic nor recognised block markers)"


def _probe(client: sso_client.SSOClient, label: str, path: str, save_as: str) -> dict:
    click.echo(f"\n[{label}] GET {path}")
    try:
        resp = client.get(path)
    except sso_window.WindowClosedError:
        raise  # not a block — let main() report "did not run"
    except sso_client.CircuitBreakerOpen as exc:
        click.echo(f"  circuit breaker open: {exc}")
        return {"label": label, "outcome": "circuit_breaker", "detail": str(exc)}
    except Exception as exc:  # httpx errors incl. 4xx after raise_for_status
        status = getattr(getattr(exc, "response", None), "status_code", None)
        click.echo(f"  FAIL status={status} {type(exc).__name__}: {exc}")
        return {"label": label, "outcome": "error", "status": status, "detail": str(exc)}
    body = resp.content
    out = OUT_DIR / save_as
    out.write_bytes(body)
    kind = _classify_body(body)
    ctype = resp.headers.get("content-type", "")
    click.echo(f"  status={resp.status_code} content-type={ctype!r} bytes={len(body)}")
    click.echo(f"  body looks like: {kind}")
    click.echo(f"  saved -> {out.relative_to(ROOT)}")
    return {
        "label": label,
        "outcome": "ok",
        "status": resp.status_code,
        "content_type": ctype,
        "bytes": len(body),
        "kind": kind,
    }


@click.command()
@click.option("--act", default="IA1965", help="Act shorthand to probe.")
def main(act: str) -> None:
    """Probe the PDF endpoint vs the HTML detail page; characterise the block."""
    refuse_fixture_env()
    click.echo(f"PDF reachability diagnostic — {sso_window.now_sgt():%Y-%m-%d %H:%M %Z}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    client = sso_client.SSOClient()
    results = []
    window_closed = False
    try:
        results.append(_probe(client, "pdf", f"/Act/{act}?ViewType=Pdf", f"diag_{act}_pdf.bin"))
        results.append(_probe(client, "html-control", f"/Act/{act}", f"diag_{act}_html.html"))
    except sso_window.WindowClosedError as exc:
        window_closed = True
        click.echo(f"\nwindow closed: {exc}")

    click.echo("\n=== VERDICT ===")
    if window_closed and not results:
        click.echo(
            "DID NOT RUN — outside the 03:00-07:00 SGT window. No conclusion. Re-run in-window."
        )
        return
    by = {r["label"]: r for r in results}
    pdf, html = by.get("pdf", {}), by.get("html-control", {})
    pdf_ok = (
        pdf.get("outcome") == "ok"
        and pdf.get("status") == 200
        and "REAL PDF" in pdf.get("kind", "")
    )
    html_blocked = html.get("outcome") in ("error", "circuit_breaker") or html.get("status") in (
        403,
        None,
    )
    if pdf_ok and html_blocked:
        click.echo(
            "PDF endpoint reachable while HTML is blocked -> PDFs on separate infra. PURSUE."
        )
    elif pdf.get("outcome") != "ok":
        click.echo(
            "PDF endpoint blocked too -> confirmed client-level WAF block. PDF route is moot."
        )
    elif pdf_ok and not html_blocked:
        click.echo(
            "Both reachable -> last night's 403s may have been transient. Re-evaluate access."
        )
    else:
        click.echo("Inconclusive — inspect the saved bodies in raw_fixtures/.")
    click.echo(f"\nResults: {results}")


if __name__ == "__main__":
    main()
