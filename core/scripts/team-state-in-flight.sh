#!/usr/bin/env bash
# Mark an agent as in-flight on a goal in the shared team state.
# Usage: bash core/scripts/team-state-in-flight.sh --agent <name> --goal-id <id> --title <text> --phase <n>
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/team-state.py" in-flight "$@"
