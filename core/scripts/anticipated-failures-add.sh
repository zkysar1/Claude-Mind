#!/usr/bin/env bash
# Anticipated-failures store (Phase 3.96 anticipatory reflection) — `add` subcommand.
# Reads a JSON record on stdin; validates + dedups on goal_id + appends (outcome=null).
# Engine: core/scripts/anticipated-failures.py. Design: world/conventions/anticipated-failures.md.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/anticipated-failures.py" add "$@"
