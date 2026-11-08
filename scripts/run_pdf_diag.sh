#!/bin/bash
# ONE-SHOT runner for the PDF-reachability diagnostic, fired by launchd at
# 03:05 SGT (sg.zeeker.statutes.pdfdiag.plist). Self-removes after running so
# it fires exactly once. The diagnostic is window-guarded; outside the window
# it exits cleanly without touching SSO.
set -uo pipefail

unset STATUTES_IGNORE_WINDOW STATUTES_NO_DELAY   # never defeat the window/pacing on a live run

REPO="/Users/houfu/Projects/zeeker-statutes"
UV="/Users/houfu/.local/bin/uv"
LOG_DIR="$REPO/.cache/spike_logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/pdfdiag_$(date +%Y%m%d_%H%M%S).log"

cd "$REPO" || exit 1
{
  echo "=== pdf diagnostic $(date) (SGT: $(TZ=Asia/Singapore date)) ==="
  "$UV" run python scripts/pdf_diagnostic.py
  echo "=== rc=$? ==="
} >> "$LOG" 2>&1

# Self-remove: one-shot, never repeats.
launchctl bootout "gui/$(id -u)/sg.zeeker.statutes.pdfdiag" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/sg.zeeker.statutes.pdfdiag.plist"
