#!/usr/bin/env bash
# Read the shared team state (full or a specific field).
# Usage: bash core/scripts/team-state-read.sh [--field <path>] [--json]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/team-state.py" read "$@"
