#!/usr/bin/env bash
# goal-field-census-ratchet.sh — advisory drift check with baseline ratchet for
# the DISTINCT top-level goal-field count ( item 3). Wired into
# /verify-learning. Makes item 1's write-time allowlist gate observable: a rise
# means the gate was bypassed, extended, or regressed.
# The stray count is REPORTED but deliberately NOT ratcheted — no available
# write path can lower it (commutative merge cannot encode a deletion); see the
# .py docstring.
# Exit 0 always unless VERIFY_LEARNING_DRIFT_HARD_GATE=1.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/goal-field-census-ratchet.py" "$@"
