#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# wm-ages — daemon-aware wrapper. Reports slot ages (minutes since last
# update/access).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. GET /v1/wm/ages
#   3. On 200: --json → verbatim body; default → reformat to CLI text
#
# Usage: bash core/scripts/wm-ages.sh [--json]
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
JSON=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --json) JSON=1; shift;;
        *) shift;;
    esac
done

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

_format_text() {
    # Reformat daemon JSON response to CLI human-readable text.
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
data = json.load(sys.stdin)
if not data:
    print('Working memory not initialized.')
    sys.exit(0)
for name, info in data.items():
    upd = str(info['minutes_since_update']) + 'm' if info['minutes_since_update'] is not None else 'never'
    acc = str(info['minutes_since_access']) + 'm' if info['minutes_since_access'] is not None else 'never'
    items = ', ' + str(info['item_count']) + ' items' if info['item_count'] is not None else ''
    print(f'  {name}: updated {upd} ago, accessed {acc} ago, {info[\"update_count\"]} writes{items}')
"
}

if [ "$JSON" = "1" ]; then
    rc=0
    rt_call GET /v1/wm/ages || rc=$?
    case $rc in
        0) exit 0;;
        2) exit 1;;
        3)
            # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
            if rt_try_autospawn; then
                rc=0
                rt_call GET /v1/wm/ages || rc=$?
                if [ "$rc" = "0" ]; then exit 0; fi
            fi
            rt_no_daemon_error "wm-ages.sh";;
        *) exit $rc;;
    esac
else
    rc=0
    RESPONSE="$(rt_call GET /v1/wm/ages)" || rc=$?
    case $rc in
        0) _format_text "$RESPONSE"; exit 0;;
        2) exit 1;;
        3)
            # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
            if rt_try_autospawn; then
                rc=0
                RESPONSE="$(rt_call GET /v1/wm/ages)" || rc=$?
                if [ "$rc" = "0" ]; then _format_text "$RESPONSE"; exit 0; fi
            fi
            rt_no_daemon_error "wm-ages.sh";;
        *) exit $rc;;
    esac
fi
