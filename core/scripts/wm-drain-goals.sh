#!/usr/bin/env bash
# DAEMON-ONLY. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# wm-drain-goals — daemon-aware wrapper. Removes every entry in a CAPTURE lane
# whose `goal_id` is in the posted set, and keeps everything else.
#
# . This is the drain site `exp_capture` and `encoding_capture` never
# had. It is deliberately NOT `wm-clear.sh`: those lanes accumulate across every
# Body and every merge, while a consumer processes one bounded batch, so a
# blanket clear would destroy entries that were never consumed — permanently,
# because capture_fast_lane's consumed-hash watermark would suppress redelivery.
#
# It is also deliberately not a read-filter-`wm-set.sh` in the caller: that is a
# full-slot overwrite of a stale snapshot and loses any concurrent append
# (guard-3881 — the predicate must be re-asserted INSIDE the write lock, which
# only the handler can do). The handler subtracts; this wrapper just carries.
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse slot arg, read the goal-id JSON array from stdin
#   3. POST /v1/wm/drain-goals?slot=<slot>
#   4. Print the handler's JSON verdict ({"removed":N,"kept":M}) on stdout
#
# Usage: echo '["g-1","g-2"]' | bash core/scripts/wm-drain-goals.sh <capture-slot>
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
SLOT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        *) SLOT="$1"; shift;;
    esac
done

if [ -z "$SLOT" ]; then
    echo "Error: slot name required. Usage: echo '[\"g-1\"]' | wm-drain-goals.sh <slot>" >&2
    exit 1
fi

# Read stdin (the goal-id array) BEFORE invoking the daemon — same ordering as
# wm-set.sh, so an autospawn retry never re-reads a consumed stdin.
BODY="$(cat)"

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="slot=$(rt_url_encode "$SLOT")"

rc=0
rt_call POST /v1/wm/drain-goals \
    --query "$QUERY" \
    --body-string "$BODY" || rc=$?

case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        if rt_try_autospawn; then
            rc=0
            rt_call POST /v1/wm/drain-goals \
                --query "$QUERY" \
                --body-string "$BODY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "wm-drain-goals.sh";;
    *) exit $rc;;
esac
