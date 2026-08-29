#!/usr/bin/env bash
# close-phase-skip-check.sh — thin wrapper for close-phase-skip-check.py
# (). Detects a close whose state-update phase never ran: the goal is
# marked completed but was never appended to loop_state.counted_goals_this_session,
# so its counter bump, journal append, iteration commit and tree-drift reset all
# silently did not happen.
#
# The shape it catches (measured 2026-08-29, zeta/cc-02, during  --
# that id is the goal that was BEING CLOSED when it happened, i.e. the
# incident VICTIM, NOT an analysis of close phases; its title is about a
# units guard and will explain nothing on its own): an
# autocompact resume re-entered the loop AT the close sequence, so learning-gate
# and productivity-check ran while verify and state-update never did. Every
# visible signal read healthy, because the phases that would have left a sentinel
# ARE the phases that did not run.
#
# Report-only by contract: there is no --apply and there must never be one. The
# remedy for a detected skip is a judgment call (re-run state-update for that
# goal, or accept the loss and record it), and an automatic re-fire would write
# loop_state on the strength of a heuristic population read.
#
# Fail-open by design: an always-run precheck lane must never block the loop, so
# any wrapper-level failure still exits 0 with a structured line (guard-614). The
# fallback carries `failed`, which the battery treats as BOTH a finding and an
# error — a lane that could not run must never fold into a clean zero.
#
# Args pass straight through ("$@") — deliberately NO bash-side arg parsing, so
# there is no `shift 2` to get wrong (guard-1224) and exactly one parser owns the
# flag surface (the .py's argparse). Add flags THERE, never here.
set -uo pipefail
_SELF="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$_SELF/_paths.sh" 2>/dev/null || true
python3 "$_SELF/close-phase-skip-check.py" "$@" \
  || echo '{"check":"close-phase-skip","applicable":false,"status":"clean","completeness":"partial","population":0,"skipped":[],"bump_noop":[],"indeterminate":[],"failed":["wrapper_failed — run py -3 core/scripts/close-phase-skip-check.py directly"]}'
exit 0
