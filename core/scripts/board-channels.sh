#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# List available board channels with message counts — daemon-aware wrapper.
# Daemon path: rt_call GET /v1/board/channels. The endpoint returns JSON;
# this wrapper reformats to match the CLI human-readable output.
# Usage: bash core/scripts/board-channels.sh
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

_translate() {
    # Reformat daemon JSON {"ok":true,"channels":[...]} to CLI output:
    #   Channels:
    #     <name>: <count> messages (last: <ts>)
    # Or "No board directory yet." / "No channels yet." for empty.
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
channels = resp.get('channels', [])
if not channels:
    print('No channels yet.')
    sys.exit(0)
print('Channels:')
for ch in channels:
    name = ch.get('name', '')
    count = ch.get('count', 0)
    last_ts = ch.get('last_timestamp')
    suffix = f' (last: {last_ts})' if last_ts else ''
    print(f'  {name}: {count} messages{suffix}')
"
}

rc=0
RESPONSE="$(rt_call GET /v1/board/channels)" || rc=$?

case $rc in
    0) _translate "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call GET /v1/board/channels)" || rc=$?
            if [ "$rc" = "0" ]; then _translate "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "board-channels.sh";;
    *) exit $rc;;
esac
