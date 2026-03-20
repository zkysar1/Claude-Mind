#!/usr/bin/env bash
# Pending background agent tracker — thin wrapper around pending-agents.py.
# Tracks dispatched background agents so the stop hook can allow stop
# when agents are pending (Gate 2.5), and the aspirations loop can
# collect results on re-engagement (Phase -0.5a).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/pending-agents.py" "$@"
