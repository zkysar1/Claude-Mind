#!/usr/bin/env bash
# Detect post-decompose-routing-audit Investigate goals (routing-mismatch /
# routing-either-resolve) whose audited TARGET goal has landed in a terminal
# status (completed/archived/skipped/superseded), making the re-stamp action
# moot. See routing-audit-target-status-sweep.py for the full docstring.
# Report-only by default; --apply marks candidates as skipped with outcome_note
# "routing-audit target resolved without action needed".
#
# Usage: routing-audit-target-status-sweep.sh [--max-age-hours N] [--apply] \
#                                             [--output json|human]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/routing-audit-target-status-sweep.py" "$@"
