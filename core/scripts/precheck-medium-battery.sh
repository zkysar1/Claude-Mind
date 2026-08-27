#!/usr/bin/env bash
# precheck-medium-battery.sh — thin wrapper for precheck-medium-battery.py
# (). One call running every MEDIUM-tier precheck lane under the budget
# meter and printing FINDINGS ONLY; see the .py docstring for the lane set, why
# medium stops short of the deferrable tier (guard-4033), and why this is a
# script rather than a better-worded imperative (guard-399 amendment 2).
#
# Fail-open by design: the battery must never block the loop, so any wrapper-level
# failure still exits 0 with a structured line (guard-614).
#
# Args pass straight through ("$@") — deliberately NO bash-side arg parsing, so
# there is no `shift 2` to get wrong (guard-1224) and exactly one parser owns the
# flag surface (the .py's argparse). Add flags THERE, never here.
set -uo pipefail
_SELF="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$_SELF/_paths.sh" 2>/dev/null || true
python3 "$_SELF/precheck-medium-battery.py" "$@" \
  || echo '[medium-battery] wrapper_failed — fall back to the per-phase lane calls (0, 0.5.0, 0.5b.0.5, 0.5b.3, 0.5b.4, 0.5c.1)'
exit 0
