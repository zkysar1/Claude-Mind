#!/usr/bin/env bash
# content-trigger-scan.sh — wrapper for content-trigger-scan.py.
#
# Sub-goal 2 of . Sources _paths.sh so WORLD_DIR is exported, then
# execs python3. See content-trigger-scan.py docstring for usage.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
exec python3 "$CORE_ROOT/scripts/content-trigger-scan.py" "$@"
