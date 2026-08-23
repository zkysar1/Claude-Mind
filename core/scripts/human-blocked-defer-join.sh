#!/usr/bin/env bash
# Surface `human_blocked` defers whose blocking condition may have arrived —
# by joining inbound human signals (answered pending-questions, board posts)
# against the defers that name them. Detective only, never mutates: see the
# module docstring in human-blocked-defer-join.py for the two signal strengths
# and why guard-1249 forbids an --apply here.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
exec python3 "$CORE_ROOT/scripts/human-blocked-defer-join.py" "$@"
