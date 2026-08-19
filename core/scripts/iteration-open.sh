#!/usr/bin/env bash
# iteration-open.sh — thin wrapper for iteration-open.py (). The
# bash-driven loop ENTRY, twin of iteration-close.sh: entry checks + the
# always-run precheck battery under the budget meter, a per-stage rc table,
# FINDINGS ONLY, selection candidates, and a terminal NEXT ACTION imperative.
# See the .py docstring for the composition contract (it dispatches the existing
# batteries and owns NO lane registry of its own) and for why a quiet run is not
# the same as a clean one (guard-4093).
#
# Args pass straight through ("$@") — no bash-side arg PARSING, so there is no
# `shift 2` to get wrong (guard-1224) and exactly one parser owns the flag
# surface (the .py's argparse). Add flags THERE, never here.
#
# THE EXIT CODE IS MODE-DEPENDENT, AND THAT IS THE POINT:
#   run mode   — FAIL-OPEN, always exit 0. Loop entry must never be blocked; an
#                entry gate that can refuse entry is worse than the drift it
#                corrects. A wrapper-level failure still prints a structured
#                line (guard-614) naming the fallback.
#   --dry-run  — PRESERVE the rc. This mode is a VERIFICATION check (it asserts
#                the lane count matches the tier table), and a check that always
#                exits 0 can never fail — the exact defect found in ,
#                where a goal chartered to hunt proxy predicates was closing on a
#                predicate that proved nothing. Forcing 0 here would make the
#                unreadable-registry case indistinguishable from a clean parse.
# The loop below is a MODE SNIFF, not arg parsing: it consumes nothing, shifts
# nothing, and leaves "$@" untouched for the .py.
set -uo pipefail
_SELF="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$_SELF/_paths.sh" 2>/dev/null || true

_DRY=0
for _a in "$@"; do [ "$_a" = "--dry-run" ] && _DRY=1; done

python3 "$_SELF/iteration-open.py" "$@"
_rc=$?

if [ "$_DRY" = "1" ]; then exit "$_rc"; fi
[ "$_rc" -ne 0 ] && echo "[iteration-open] wrapper_failed — fall back to the batteries directly: orchestrator-entry-battery.sh, precheck-sentinel-battery.sh, precheck-always-run-battery.sh --apply, then goal-selector.sh"
exit 0
