#!/usr/bin/env bash
# stall-goal-filer.sh
#
# Thin bash wrapper around stall-goal-filer.py. Matches the pattern used by
# other world-aware core scripts (aspirations-add-goal.sh, stop-hook-analyze.sh):
# source _paths.sh so WORLD_DIR/META_DIR are resolved, cd to PROJECT_ROOT, then
# invoke Python with the platform shim.
#
# Parameters are passed through verbatim:
#   --agent <name>   override MIND_AGENT (default: current session binding)
#   --dry-run        print what would be filed without writing

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/stall-goal-filer.py" "$@"
