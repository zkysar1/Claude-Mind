#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# tree-encoding-drift-gate.sh — bash-enforcement of Phase 8.0.5/8.0.6 ().
#
# Fires from iteration-close.sh do_state_update at the END of the state-update
# phase. Single writer for loop_state.signals.goals_since_last_tree_update.
#
# Behavior:
#   - Increment goals_since_last_tree_update by 1.
#   - If counter >= tree_encoding_drift_threshold (config; default 3):
#       set force_tree_encoding="true" (own slot)
#       reset counter to 0
#   - Else: persist incremented counter only.
#
# Replaces LLM-residue Phase 8.0.5/8.0.6 in core/config/aspirations-loop-digest.md.
# The LLM-residue path drifted (counter=8, sentinel=false observed iter-101
# alpha session 58). rb-428 family — bash safety net for LLM-only steps.
#
# Fail-open: ANY error → exit 0 with stderr noise. Caller (iteration-close.sh)
# wraps with `|| echo WARN`, so a single failure never blocks state-update.

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$(cd "$(dirname "$0")" && pwd)/_platform.sh"

exec python3 "$CORE_ROOT/scripts/tree-encoding-drift-gate.py" "$@"
