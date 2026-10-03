#!/usr/bin/env bash
# groom-bite.sh — this agent's candidate-grooming slice (, B4).
#
#   groom-bite.sh [--source world|agent] [--roster a,b,c]
#
# Prints JSON: the <= groom_bite OLDEST candidates this agent owns by affinity
# (intended_agent match -> role top-class match -> hash partition), minus any
# touched within touch_stale_hours; the promote budget left in this firing; and
# where the config came from. Spec: goal-intake-management.md §5 (BINDING).
# Engine, exit codes and rationale: groom.py.
set -euo pipefail
GROOM_HOME="$(cd "$(dirname "$0")" && pwd)"   # not SCRIPT_DIR: _paths.sh rebinds it (guard-5093)
source "$GROOM_HOME/_paths.sh"
cd "$PROJECT_ROOT"
# A plumbing fault exits 9, outside every verdict code the engine returns.
[ -f "$GROOM_HOME/groom.py" ] || { echo "groom-bite.sh: groom.py missing beside this wrapper" >&2; exit 9; }
exec python3 "$GROOM_HOME/groom.py" bite "$@"
