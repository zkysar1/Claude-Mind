#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# experience-archive — daemon-aware wrapper. Sweeps old/low-utility
# experience records to archive.
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. POST /v1/experience/archive-sweep
#   3. On 200, extract .archived from JSON response and print as bare integer
#
# Usage: bash core/scripts/experience-archive.sh
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

_translate() {
    # Extract .archived from daemon JSON response and print as bare integer,
    # matching the CLI's `print(str(len(to_archive)))` / `print("0")` output.
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
print(resp.get('archived', 0))
"
}

rc=0
RESPONSE="$(rt_call POST /v1/experience/archive-sweep)" || rc=$?

case $rc in
    0) _translate "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/experience/archive-sweep)" || rc=$?
            if [ "$rc" = "0" ]; then _translate "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "experience-archive.sh";;
    *) exit $rc;;
esac
