#!/usr/bin/env bash
# One-command operator status — replaces the 6-probe re-entry ritual.
# Usage:
#   bash core/scripts/status.sh              # pretty box
#   bash core/scripts/status.sh --json       # machine-readable JSON
#   bash core/scripts/status.sh --field X    # single field (dotted path)
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/status.py" "$@"
