#!/usr/bin/env bash
# Update a field in the shared team state.
# Usage: bash core/scripts/team-state-update.sh --field <path> --value '<json>' [--operation set|append|remove]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/team-state.py" update "$@"
