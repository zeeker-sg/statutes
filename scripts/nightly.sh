#!/bin/bash
# Nightly statutes run — the entrypoint the personal agent invokes on its timer.
#
# The agent owns scheduling; this script owns the pipeline:
#   zeeker build (--sync-from-s3) -> sanity checks (gate) -> zeeker deploy
#
# TIMING CONSTRAINT FOR THE AGENT'S TIMER (advisory; code enforces it anyway):
# live extraction from sso.agc.gov.sg is only permitted 03:00-07:00 SGT
# (SSO Terms of Use cl 13(d)(i)). Start the run shortly after 03:00 SGT.
# The in-code window guard refuses requests outside the window and stops
# issuing new ones after 06:45 SGT, so a mistimed start degrades to a no-op
# rather than a violation.
#
# Usage:
#   scripts/nightly.sh            # build + sanity checks only (no deploy)
#   scripts/nightly.sh --deploy   # build + sanity checks + zeeker deploy
#
# Deploy is opt-in until the first human review of extracted data signs off
# (lessons-learned #13); after that the agent should pass --deploy.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1

# Belt-and-braces ToU guard: fixture-only knobs must never leak into a live run.
unset STATUTES_IGNORE_WINDOW STATUTES_NO_DELAY

# Snapshot the previous DB (if present) so sanity checks can enforce the
# append-mostly row-count invariant against last night's state.
if [ -f statutes.db ]; then
  cp statutes.db statutes.previous.db
fi

echo "=== zeeker build $(TZ=Asia/Singapore date '+%Y-%m-%d %H:%M %Z') ==="
uv run zeeker build --sync-from-s3 --setup-fts
BUILD_RC=$?
if [ $BUILD_RC -ne 0 ]; then
  echo "BUILD FAILED (rc=$BUILD_RC) - not deploying" >&2
  exit $BUILD_RC
fi

echo "=== sanity checks ==="
if ! uv run python scripts/sanity_checks.py; then
  echo "SANITY CHECKS FAILED - not deploying" >&2
  exit 1
fi

if [ "${1:-}" = "--deploy" ]; then
  echo "=== zeeker deploy ==="
  uv run zeeker deploy
else
  echo "=== deploy skipped (pass --deploy to ship) ==="
fi
