#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# tree-propagate — daemon-aware wrapper. Propagates confidence up the
# parent chain from a named node.
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. POST /v1/tree/write  {"op":"propagate","key":"<KEY>"}
#   3. On 200, translate daemon JSON to CLI-compat stdout
#
# Usage: bash core/scripts/tree-propagate.sh <KEY>
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
KEY=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        *) KEY="$1"; shift;;
    esac
done

if [ -z "$KEY" ]; then
    echo "Usage: tree-propagate.sh <KEY>" >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

BODY="{\"op\":\"propagate\",\"key\":$($(rt_python_launcher) -c "import json,sys;print(json.dumps(sys.argv[1]))" "$KEY")}"

_translate() {
    # CLI prints: {"source_node":..., "ancestors_updated":..., "capability_changes":...}
    # with indent=2, ensure_ascii=False. Daemon returns those plus "ok" and "op".
    # Strip the extra fields to match CLI byte-compat.
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
out = {
    'source_node': resp['source_node'],
    'ancestors_updated': resp['ancestors_updated'],
    'capability_changes': resp['capability_changes'],
}
print(json.dumps(out, indent=2, ensure_ascii=False))
"
}

rc=0
RESPONSE="$(rt_call POST /v1/tree/write \
    --body-string "$BODY")" || rc=$?

case $rc in
    0) _translate "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/tree/write \
                --body-string "$BODY")" || rc=$?
            if [ "$rc" = "0" ]; then _translate "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "tree-propagate.sh";;
    *) exit $rc;;
esac
