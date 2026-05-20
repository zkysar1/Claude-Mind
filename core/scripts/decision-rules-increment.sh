#!/usr/bin/env bash
# decision-rules-increment.sh — E8 bash wrapper for the applied-counter
# increment. Mirrors the pattern of decision-rules-append.sh.
#
# Usage:
#   echo '{"rules": ["IF X THEN Y", "IF A THEN B"]}' \
#     | bash decision-rules-increment.sh --node-path <path>
#
# Returns non-zero only when no rules matched (caller should know what it
# cited). Idempotent across calls — re-running with the same payload bumps
# the counter again, which is the intended behavior (the counter measures
# invocations, not unique events).

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/decision-rules-increment.py" "$@"
