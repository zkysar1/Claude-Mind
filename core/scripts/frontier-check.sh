#!/usr/bin/env bash
# frontier-check — print the claimable-frontier census (read-only).
# Same implementation as agent-watchdog's DependencyFunnelProbe (_frontier.py).
#   bash core/scripts/frontier-check.sh [--json] [--lookback-hours N]
# Exit 0: frontier > 0 (or nothing pending); 2: frontier 0 with gated goals.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/frontier-check.py" "$@"
