#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# wm-clear-identity — daemon-aware wrapper. Clears SESSION_IDENTITY_FIELDS
# from working memory. Authorized caller: /stop graceful-stop D4.5.
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. POST /v1/wm/clear-identity (no query params, no body)
#   3. On 200, translate daemon JSON to CLI-compat stdout
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

_translate() {
    # Reproduce CLI-compat stdout from daemon JSON response.
    # Daemon returns: {"ok":true,"cleared":[...],"detail":"..."}
    # CLI printed one of:
    #   "Working memory not initialized; nothing to clear."
    #   "Session-identity fields already clear."
    #   "Cleared session-identity fields: session_start."
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
cleared = resp.get('cleared', [])
detail = resp.get('detail', '')
if not cleared and 'not initialized' in detail:
    print('Working memory not initialized; nothing to clear.')
elif not cleared:
    print('Session-identity fields already clear.')
else:
    print('Cleared session-identity fields: ' + ', '.join(cleared) + '.')
"
}

rc=0
RESPONSE="$(rt_call POST /v1/wm/clear-identity)" || rc=$?

case $rc in
    0) _translate "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/wm/clear-identity)" || rc=$?
            if [ "$rc" = "0" ]; then _translate "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "wm-clear-identity.sh";;
    *) exit $rc;;
esac
