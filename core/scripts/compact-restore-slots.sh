#!/usr/bin/env bash
# Restore all WM slots from compact checkpoint.
# Called by Phase -0.5c of the aspirations loop.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/compact-restore-slots.py" "$@"
