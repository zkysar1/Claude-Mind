#!/usr/bin/env bash
# goal-field-census-ratchet.sh — advisory drift check with baseline ratchet for
# the count of goal-field names on world goals that are neither registered nor
# declared strays ( item 3). Wired into /verify-learning. Makes item
# 1's write-time allowlist gate observable: a rise means a writer set a name
# without passing the gate.
# The stray count, the total name count and the bound agent's own queue are
# REPORTED but deliberately NOT ratcheted; see the .py docstring.
# Exit 0 always unless VERIFY_LEARNING_DRIFT_HARD_GATE=1.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/goal-field-census-ratchet.py" "$@"
