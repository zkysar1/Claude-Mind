#!/usr/bin/env bash
# Shell wrapper for trigger-firings.py ().
# Usage: bash core/scripts/trigger-firings.sh record <trigger_id> [--context <json>]
#        bash core/scripts/trigger-firings.sh report [--since 24h] [--trigger T1] [--json]
set -euo pipefail
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

source "$CORE_ROOT/scripts/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/trigger_firings.py" "$@"
