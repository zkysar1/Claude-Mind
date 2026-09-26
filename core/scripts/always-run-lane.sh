#!/usr/bin/env bash
# Always-run lane (): names at most one recurring goal that opted in
# with `dispatch_lane: always-run[:<agent>]` and whose interval has elapsed, so
# aspirations-select can dispatch it OUTSIDE the scorer draw. It never overrides
# an eligibility gate: select honors the pin only when goal-selector.sh also lists
# the goal. Exit 0 always (fail-open); one JSON object on stdout. See the .py
# docstring for the contract.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/always-run-lane.py" "$@"
