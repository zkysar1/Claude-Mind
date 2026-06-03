#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Update a field in the shared team state — daemon-aware wrapper.
# Daemon path: rt_call POST /v1/team-state/update.
# Usage: bash core/scripts/team-state-update.sh --field <path> --value '<json>' [--operation set|append|remove]
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# Value-arg pattern: "${2-}" + safe shift; see _runtime.sh / tree-read.sh.
FIELD=""; VALUE=""; OPERATION=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --field)     FIELD="${2-}";     shift $(( $# >= 2 ? 2 : 1 ));;
        --value)     VALUE="${2-}";     shift $(( $# >= 2 ? 2 : 1 ));;
        --operation) OPERATION="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --author)    shift $(( $# >= 2 ? 2 : 1 ));;  # daemon uses X-Mind-Agent header
        *) shift;;
    esac
done

# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY=""
_append_q() { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="$1"; }
[ -n "$FIELD" ]     && _append_q "field=$(rt_url_encode "$FIELD")"
[ -n "$VALUE" ]     && _append_q "value=$(rt_url_encode "$VALUE")"
[ -n "$OPERATION" ] && _append_q "operation=$(rt_url_encode "$OPERATION")"

_translate() {
    # CLI printed: "Updated {field}" — extract field from daemon JSON response.
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
print('Updated ' + resp['field'])
"
}

rc=0
RESPONSE="$(rt_call POST /v1/team-state/update --query "$QUERY")" || rc=$?

case $rc in
    0) _translate "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/team-state/update --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then _translate "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "team-state-update.sh";;
    *) exit $rc;;
esac
