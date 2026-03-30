#!/usr/bin/env bash
# meta-generations.sh — Strategy generation tracking
# Usage:
#   meta-generations.sh snapshot
#   meta-generations.sh close [--metrics '<json>']
#   meta-generations.sh open
#   meta-generations.sh update --learning-value <v>
#   meta-generations.sh status
#   meta-generations.sh history [--top N]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/meta-generations.py" "$@"
