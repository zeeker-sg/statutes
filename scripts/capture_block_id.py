"""One-shot: record the firewall's request/incident ID for AGC's whitelisting form.

AGC's IT Division (email of 26 Nov 2026) asked for the "Request ID associated with
the bot/crawler" before they consider temporarily whitelisting ZeekerBot. That ID
is printed on the Imperva/Incapsula block page, which our client never saved.

This sends exactly ONE request, as the normal client would:

- ``GET https://sso.agc.gov.sg/robots.txt``, the lightest page on the site;
- the honest ``ZeekerBot/1.0`` user agent from ``sso_client``, with no other
  identity and no retry;
- only inside the cl 13(d)(i) window (03:00-06:45 SGT; refuses otherwise);
- and refuses to run when the fixture-only env vars are set, like the other
  live scripts.

It saves the status, response headers and body under ``.cache/block_capture/``
and prints any incident ID it finds. Once one capture exists it does nothing,
so if it's scheduled nightly by mistake it can't keep sending requests.

Run it on the machine whose IP will be whitelisted (zeeker-ml), so the
request ID and the source IP that AGC sees belong together.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path

import click
import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "resources"))

import sso_client  # noqa: E402
import sso_window  # noqa: E402

FORBIDDEN_ENV_VARS = ("STATUTES_IGNORE_WINDOW", "STATUTES_NO_DELAY")
TARGET_URL = f"{sso_client.BASE_URL}/robots.txt"
CAPTURE_DIR = ROOT / ".cache" / "block_capture"

# Incapsula block pages say e.g. "Incapsula incident ID: 1234000560012345678-123456789012345".
_INCIDENT_RE = re.compile(r"incident\s*id[^0-9A-Za-z]{0,10}([0-9A-Za-z][0-9A-Za-z\-]{5,})", re.I)
# Response headers that carry Imperva/Incapsula request identifiers.
ID_HEADERS = ("x-iinfo", "x-cdn", "x-request-id", "x-amz-cf-id")


def refuse_fixture_env() -> None:
    present = [name for name in FORBIDDEN_ENV_VARS if name in os.environ]
    if present:
        click.echo(
            f"ERROR: {', '.join(present)} set. This script sends a LIVE request to "
            "sso.agc.gov.sg; the window guard must stay active (SSO ToU cl 13(d)). "
            "Unset and re-run inside 03:00-07:00 SGT.",
            err=True,
        )
        sys.exit(2)


def find_incident_ids(body: str, headers: httpx.Headers) -> list[str]:
    found = [m.group(1) for m in _INCIDENT_RE.finditer(body)]
    found += [f"{h}: {headers[h]}" for h in ID_HEADERS if h in headers]
    return list(dict.fromkeys(found))


def existing_captures(capture_dir: Path = CAPTURE_DIR) -> list[Path]:
    return sorted(capture_dir.glob("capture_*.txt")) if capture_dir.exists() else []


def format_capture(sent_at: datetime, resp: httpx.Response, ids: list[str]) -> str:
    lines = [
        f"sent_at_sgt: {sent_at.isoformat()}",
        f"url: {resp.request.url}",
        f"request_user_agent: {resp.request.headers.get('user-agent')}",
        f"status: {resp.status_code} {resp.reason_phrase}",
        "incident_ids: " + (" | ".join(ids) if ids else "(none found)"),
        "",
        "--- response headers ---",
        *(f"{k}: {v}" for k, v in resp.headers.multi_items()),
        "",
        "--- response body ---",
        resp.text,
    ]
    return "\n".join(lines)


def capture(transport: httpx.BaseTransport | None = None, capture_dir: Path = CAPTURE_DIR) -> int:
    """Send the single request and save it. Returns a process exit code."""
    done = existing_captures(capture_dir)
    if done:
        click.echo(f"Already captured ({done[-1].name}); sending nothing.")
        return 0
    try:
        sso_window.require_window()
    except sso_window.WindowClosedError as exc:
        click.echo(f"Window closed; sending nothing. {exc}")
        return 0

    headers = {"User-Agent": sso_client.USER_AGENT, "Accept": "text/plain,text/html"}
    sent_at = sso_window.now_sgt()
    with httpx.Client(
        headers=headers,
        timeout=sso_client.REQUEST_TIMEOUT,
        follow_redirects=False,
        transport=transport,
    ) as client:
        try:
            resp = client.get(TARGET_URL)
        except httpx.TransportError as exc:
            click.echo(f"Request failed before any response: {exc!r}", err=True)
            return 1

    ids = find_incident_ids(resp.text, resp.headers)
    capture_dir.mkdir(parents=True, exist_ok=True)
    out = capture_dir / f"capture_{sent_at:%Y%m%d_%H%M%S}.txt"
    sso_client.cache_write(out, format_capture(sent_at, resp, ids))

    click.echo(f"status={resp.status_code} saved={out}")
    click.echo("incident IDs: " + (", ".join(ids) if ids else "(none found; read the saved file)"))
    return 0


@click.command()
def main() -> None:
    """Send one window-guarded ZeekerBot request and save the firewall's response."""
    refuse_fixture_env()
    sys.exit(capture())


if __name__ == "__main__":
    main()
