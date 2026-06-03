#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# wm-set — daemon-aware wrapper. Sets a working-memory slot value
# from stdin (JSON or scalar).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse slot arg + optional --override-merge-gate
#   3. Read stdin body (value) BEFORE invoking daemon
#   4. POST /v1/wm/set?slot=<slot>[&override_merge_gate=<justification>]
#   5. On 200, print nothing (CLI byte-compat: cmd_set prints nothing on success)
#
# Usage: echo '"value"' | bash core/scripts/wm-set.sh <slot> [--override-merge-gate <justification>]
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
SLOT=""
OVERRIDE_MERGE_GATE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --override-merge-gate) OVERRIDE_MERGE_GATE="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        *) SLOT="$1"; shift;;
    esac
done

if [ -z "$SLOT" ]; then
    echo "Error: slot name required. Usage: wm-set.sh <slot> [--override-merge-gate <justification>]" >&2
    exit 1
fi

# Read stdin (the value to set) BEFORE invoking the daemon.
BODY="$(cat)"

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="slot=$(rt_url_encode "$SLOT")"
[ -n "$OVERRIDE_MERGE_GATE" ] && QUERY+="&override_merge_gate=$(rt_url_encode "$OVERRIDE_MERGE_GATE")"

rc=0
rt_call POST /v1/wm/set \
    --query "$QUERY" \
    --body-string "$BODY" > /dev/null || rc=$?

case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call POST /v1/wm/set \
                --query "$QUERY" \
                --body-string "$BODY" > /dev/null || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "wm-set.sh";;
    *) exit $rc;;
esac
