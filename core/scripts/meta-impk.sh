#!/usr/bin/env bash
# meta-impk.sh — Compute or snapshot improvement velocity (imp@k)
# Usage:
#   meta-impk.sh compute --window <k> --metric <name>
#   meta-impk.sh snapshot --goal-id <id> --learning-value <v>
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/meta-impk.py" "$@"
