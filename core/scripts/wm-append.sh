#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# wm-append — daemon-aware wrapper. Appends a JSON item to an array slot
# in working memory.
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Read stdin body (JSON item to append)
#   3. POST /v1/wm/append?slot=<name>
#   4. On 200, print nothing (CLI printed nothing on success)
#
# Usage: echo '{"key":"value"}' | bash core/scripts/wm-append.sh <slot>
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
    echo "Error: slot name required. Usage: wm-append.sh <slot>" >&2
    exit 1
fi

# Read stdin (the JSON item) BEFORE invoking the daemon.
BODY="$(cat)"

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

rc=0
rt_call POST /v1/wm/append \
    --query "slot=$(rt_url_encode "$SLOT")" \
    --body-string "$BODY" > /dev/null || rc=$?

case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call POST /v1/wm/append \
                --query "slot=$(rt_url_encode "$SLOT")" \
                --body-string "$BODY" > /dev/null || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "wm-append.sh";;
    *) exit $rc;;
esac
