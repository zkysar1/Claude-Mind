#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# aspirations-recover-recurring — daemon-aware wrapper (PR 53).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse --source flag
#   3. POST /v1/aspirations/recover-recurring with source as query param
#   4. On 200, print recovered count + goal details to stdout
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
SOURCE_VAL="world"
declare -a PASSTHROUGH_SOURCE=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            SOURCE_VAL="${2-}"
            PASSTHROUGH_SOURCE=(--source "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        *) shift;;
    esac
done

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="source=${SOURCE_VAL}"

rc=0
RESPONSE="$(rt_call POST /v1/aspirations/recover-recurring \
    --query "$QUERY")" || rc=$?

case $rc in
    0)
        # 200: print the JSON response (matches legacy CLI json.dumps shape).
        echo "$RESPONSE"
        exit 0;;
    2)
        # Daemon answered 4xx/5xx; body already written to stderr by rt_curl.
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/aspirations/recover-recurring \
                --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then echo "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "aspirations-recover-recurring.sh";;
    *)
        exit $rc;;
esac
