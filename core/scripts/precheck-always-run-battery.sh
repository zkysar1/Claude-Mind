#!/usr/bin/env bash
# precheck-always-run-battery.sh — thin wrapper for precheck-always-run-battery.py
# (). One call running every STANDALONE always-run precheck lane under
# the budget meter and printing FINDINGS ONLY; see the .py docstring for the lane
# set, the meter-name-vs-script-name trap, and why a quiet run is not the same as
# a clean one (guard-4093).
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
python3 "$_SELF/precheck-always-run-battery.py" "$@" \
  || echo '[always-run-battery] wrapper_failed — fall back to the per-phase lane calls (0.5b.1b, 0.5b.1c, 0.5b.2, 0.5b.2b, 0.5g.7)'
exit 0
