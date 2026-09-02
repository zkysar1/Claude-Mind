#!/usr/bin/env bash
# goal-close-risk-tier.sh — wrapper for the close-review risk-tier classifier ().
# Pure exec passthrough: sources _paths.sh so MIND_WORLD / WORLD_PATH resolve from the
# per-agent local-paths.conf, then execs the classifier with "$@" unchanged. Invoke THIS,
# never the bare .py — a bare py -3 has STORAGE_BACKEND but no mappable world root, so
# store reads silently take the local-mirror branch (guard-3864 / rb-7918).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

#  cygpath conversion -- see peer-board-post.sh for the full rationale.
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi

exec python3 "$SCRIPT_DIR_NATIVE/goal-close-risk-tier.py" "$@"
