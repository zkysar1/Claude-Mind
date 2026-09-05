#!/usr/bin/env bash
# stalled-goal-ratchet.sh — advisory drift check with baseline ratchet for goals
# that have been NON-EXECUTABLE too long, class-agnostically.
#
# Every per-class escape hatch in the fleet (the 120h defer TTL, handoff-aging,
# dependency-timeout, blocked-signal-resolution, precondition-recheck) asks "is
# THIS block still valid?" and answers correctly. None bounds TOTAL time, so a
# goal can be correctly blocked forever and be nobody's alarm. This counts them.
#
# Ratchet, not gate, per rb-8533 (unfixable-debt detectors ratchet; a gate here
# would refuse legitimate writes and be overridden into irrelevance).
# Exit 0 always unless VERIFY_LEARNING_DRIFT_HARD_GATE=1.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/stalled-goal-ratchet.py" "$@"
