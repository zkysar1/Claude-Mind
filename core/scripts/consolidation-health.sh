#!/usr/bin/env bash
# consolidation-health.sh — compute + write consolidation_health WM slot.
# Thin wrapper; all logic in consolidation-health.py.
# Invoked from aspirations-precheck Phase 0.5 every iteration.
# See the .py docstring for schema and consumers.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/consolidation-health.py" "$@"
