#!/usr/bin/env bash
# tree-accuracy-sync.sh — thin wrapper around tree-accuracy-sync.py.
# See the .py for design rationale (, , rb-292).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
cd "$PROJECT_ROOT"
exec python3 "$CORE_ROOT/scripts/tree-accuracy-sync.py" "$@"
