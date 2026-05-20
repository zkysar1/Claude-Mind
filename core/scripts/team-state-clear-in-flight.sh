#!/usr/bin/env bash
# Remove the in_flight block from an agent's status in the shared team state.
# Usage: bash core/scripts/team-state-clear-in-flight.sh --agent <name>
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/team-state.py" clear-in-flight "$@"
