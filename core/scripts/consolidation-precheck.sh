#!/usr/bin/env bash
# Consolidation precheck — check all encoding queues in one shot.
# Returns JSON verdict: FULL (encoding work exists) or FAST (queues empty).
# Called by aspirations orchestrator and /stop before deciding whether to
# invoke full /aspirations-consolidate or load the housekeeping digest.
#
# Output: single-line JSON to stdout (see consolidation-precheck.py)
# Exit: always 0 (errors reported in verdict field as FULL fallback)
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"

exec python3 "$CORE_ROOT/scripts/consolidation-precheck.py"
