#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
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
