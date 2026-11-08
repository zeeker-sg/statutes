#!/bin/bash
# Nightly Phase-0 spike runner, fired by launchd at 03:05 SGT
# (~/Library/LaunchAgents/sg.zeeker.statutes.spike.plist).
#
# Safe to run at any hour: both scripts call the sso_window guard before every
# live request and exit 0 with "window closed, checkpointed" outside
# 03:00-07:00 SGT. Remove the launchd agent once Phase 0 is done:
#   launchctl bootout gui/$(id -u)/sg.zeeker.statutes.spike
#   rm ~/Library/LaunchAgents/sg.zeeker.statutes.spike.plist
set -uo pipefail

# Belt-and-braces ToU guard: the fixture-only env knobs must never leak into
# a live-fetching run (each Python script ALSO hard-refuses if it sees them).
unset STATUTES_IGNORE_WINDOW STATUTES_NO_DELAY

REPO="/Users/houfu/Projects/zeeker-statutes"
UV="/Users/houfu/.local/bin/uv"
LOG_DIR="$REPO/.cache/spike_logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/spike_$(date +%Y%m%d_%H%M%S).log"

cd "$REPO" || exit 1
{
  echo "=== nightly spike run $(date) (SGT: $(TZ=Asia/Singapore date)) ==="
  # Diverse fixture set for parser validation: short act, interpretation,
  # evidence (illustrations), companies (schedules), penal code (deep
  # nesting), constitution (odd subtype), income tax (huge), CPC, employment.
  "$UV" run python scripts/fetch_fixtures.py \
    --act AA1928 --act IA1965 --act EA1893 --act CoA1967 \
    --act PC1871 --act CONS1963 --act ITA1947 --act CPC2010 --act EmA1968
  FETCH_RC=$?
  "$UV" run python scripts/phase0_spike.py
  SPIKE_RC=$?
  echo "=== fetch_fixtures rc=$FETCH_RC phase0_spike rc=$SPIKE_RC ==="

  # VALIDATION BUILD (local only, nothing deployed): the exact pipeline the
  # nightly CI would run, writing ./statutes.db (gitignored) for human
  # inspection before any deploy is enabled. Window guard + 06:45 hard stop
  # bound the request budget; extraction failures quarantine, never deploy.
  echo "=== local validation build ==="
  "$UV" run zeeker build
  BUILD_RC=$?
  "$UV" run python scripts/sanity_checks.py || echo "sanity checks flagged issues (see above)"
  echo "=== zeeker build rc=$BUILD_RC ==="

  # Commit whatever landed; push if credentials are available to launchd.
  # Each path added separately: git add aborts wholesale on a missing pathspec.
  git add tests/fixtures 2>/dev/null
  git add docs/phase0-findings.md 2>/dev/null
  if ! git diff --cached --quiet; then
    git commit -m "Nightly Phase-0 spike: fixtures + findings ($(TZ=Asia/Singapore date +%Y-%m-%d))

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
    git push origin main || echo "push failed (will push next session)"
  else
    echo "nothing new to commit"
  fi
  echo "=== done $(date) ==="
} >> "$LOG" 2>&1
