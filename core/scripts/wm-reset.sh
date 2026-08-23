#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# wm-reset — daemon-aware wrapper. Resets working memory to template state,
# preserving session-identity fields and cadence trackers.
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. POST /v1/wm/reset (no query, no body)
#   3. On 200, translate daemon JSON to CLI-compat stdout line
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
    # CLI contract:
    #   "Working memory reset to template state (N slots; preserved: X; M cadence trackers)."
    #   "Working memory reset to template state (N slots)."
    # Daemon returns: {"ok": true, "preserved_identity": [...], "preserved_cadence": N}
    # NOTE: daemon response lacks slot count; translation omits it (reformat).
    # No caller parses this output — it is informational only.
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
identity = resp.get('preserved_identity', [])
cadence = resp.get('preserved_cadence', 0)
surviving = resp.get('preserved_surviving', [])
parts = []
if identity:
    parts.append(', '.join(identity))
if cadence:
    parts.append(f'{cadence} cadence trackers')
if surviving:
    parts.append('reset-surviving: ' + ', '.join(surviving))
if parts:
    print(f\"Working memory reset to template state (preserved: {'; '.join(parts)}).\")
else:
    print('Working memory reset to template state.')
"
}

rc=0
RESPONSE="$(rt_call POST /v1/wm/reset)" || rc=$?

case $rc in
    0) _translate "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/wm/reset)" || rc=$?
            if [ "$rc" = "0" ]; then _translate "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "wm-reset.sh";;
    *) exit $rc;;
esac
