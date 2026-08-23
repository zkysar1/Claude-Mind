#!/usr/bin/env bash
# Write a per-session summary into the bound session dir.
# Phase 2.6.D — makes per-session dirs self-describing.
#
# Usage:
#   bash core/scripts/session-summary-write.sh \
#       --sid <SID> --agent <NAME> --reason <graceful-stop|consolidate|crash-recovered|unknown> \
#       [--iterations N] [--goals-filed N] [--tree-writes N] [--uncommitted-paths N]
#
# Best-effort: if the session dir doesn't exist, exits 0 silently.
# Returns 2 on validation error.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
py -3 "$SCRIPT_DIR/session-summary-write.py" "$@" \
    || python3 "$SCRIPT_DIR/session-summary-write.py" "$@"
