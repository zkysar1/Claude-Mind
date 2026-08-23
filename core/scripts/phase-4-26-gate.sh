#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# phase-4-26-gate.sh — bash wrapper for Phase 4.26 explicit-feedback gate.
# Refuses goal completion when retrieval ran but produced zero positive
# signal (utilization_method=all_noise, or method=infer with helpful=0).
# rb-472 / guard-415 writer-layer enforcement; sibling rb-428 wrapper.
#
# Usage:
#   bash phase-4-26-gate.sh --goal <goal-id>
#   bash phase-4-26-gate.sh --goal <goal-id> --no-retrieval-applicable "<reason>"
#
# Exits 0 on pass (or override-logged), 1 on block, 2 on usage error.
# See phase-4-26-gate.py docstring for full verdict matrix.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/phase-4-26-gate.py" "$@"
