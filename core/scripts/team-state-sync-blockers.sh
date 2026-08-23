#!/usr/bin/env bash
# Thin bash wrapper around team-state-sync-blockers.py. See the .py for the
# full contract (drift guard for team-state.yaml critical_blockers).
# Invoked from /prime Phase 5.45 and aspirations-state-update Step 3.5.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/team-state-sync-blockers.py" "$@"
