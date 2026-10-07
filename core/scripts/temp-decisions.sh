#!/usr/bin/env bash
# temp-decisions.sh — thin wrapper for temp_decisions.py, the decision log that
# governs agents/<agent>/temp/ (user directive 2026-10-05: nothing in temp/ is
# deleted until a review has seen it, and every decision is recorded). Local,
# non-daemon; see the .py header for the log format and the commands.
#
# Usage: temp-decisions.sh [--temp-dir PATH] <pending|bulk-junk|decide|deletable|
#                           log-deleted|show|pressure> [options]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/temp_decisions.py" "$@"
