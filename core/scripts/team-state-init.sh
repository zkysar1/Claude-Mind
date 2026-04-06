#!/usr/bin/env bash
# Initialize team-state.yaml if it doesn't exist.
# Usage: bash core/scripts/team-state-init.sh
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/team-state.py" init "$@"
