#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Read experience records — daemon-aware wrapper.
#
# Migrated for Phase B PR 4. Daemon path: rt_call /v1/experience/read.
#
# NOTE: --validate is NOT served by the daemon (cross-file scan). The wrapper
# detects --validate and falls straight through to the fallback path.
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

declare -a FLAG_KEYS=()
declare -a PASSTHROUGH=()
REC_ID=""
CATEGORY=""
GOAL=""
HYPOTHESIS=""
TYP=""
MOST=""
LEAST=""
RECENT=""
VALIDATE=0

# When --most-retrieved / --least-retrieved / --recent take an OPTIONAL int
# (CLI nargs='?', const=10), only consume the next arg if it's numeric.
_take_optional_int() {
    if [ $# -gt 0 ] && [[ "$1" =~ ^[0-9]+$ ]]; then
        echo "$1"
        return 0
    fi
    echo "10"
    return 1
}

# Value-arg pattern: "${2-}" + safe shift; see retrieve.sh for rationale.
# (--most-retrieved / --least-retrieved / --recent below already check $#.)
while [[ $# -gt 0 ]]; do
    case "$1" in
        --id)
            REC_ID="${2-}"; PASSTHROUGH+=(--id "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --category)
            CATEGORY="${2-}"; PASSTHROUGH+=(--category "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --goal)
            GOAL="${2-}"; PASSTHROUGH+=(--goal "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --hypothesis)
            HYPOTHESIS="${2-}"; PASSTHROUGH+=(--hypothesis "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --type)
            TYP="${2-}"; PASSTHROUGH+=(--type "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --most-retrieved)
            if [ $# -gt 1 ] && [[ "$2" =~ ^[0-9]+$ ]]; then
                MOST="$2"; PASSTHROUGH+=("$1" "$2"); shift 2
            else
                MOST="10"; PASSTHROUGH+=("$1"); shift
            fi;;
        --least-retrieved)
            if [ $# -gt 1 ] && [[ "$2" =~ ^[0-9]+$ ]]; then
                LEAST="$2"; PASSTHROUGH+=("$1" "$2"); shift 2
            else
                LEAST="10"; PASSTHROUGH+=("$1"); shift
            fi;;
        --recent)
            if [ $# -gt 1 ] && [[ "$2" =~ ^[0-9]+$ ]]; then
                RECENT="$2"; PASSTHROUGH+=("$1" "$2"); shift 2
            else
                RECENT="10"; PASSTHROUGH+=("$1"); shift
            fi;;
        --summary)  FLAG_KEYS+=(summary); PASSTHROUGH+=("$1"); shift;;
        --archive)  FLAG_KEYS+=(archive); PASSTHROUGH+=("$1"); shift;;
        --meta)     FLAG_KEYS+=(meta);    PASSTHROUGH+=("$1"); shift;;
        --validate)
            VALIDATE=1; FLAG_KEYS+=(validate); PASSTHROUGH+=("$1"); shift;;
        *)
            PASSTHROUGH+=("$1"); shift;;
    esac
done

source "$CORE_ROOT/scripts/_runtime.sh"

QUERY=""
[ -n "$REC_ID" ]     && QUERY="id=$(rt_url_encode "$REC_ID")"
[ -n "$CATEGORY" ]   && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="category=$(rt_url_encode "$CATEGORY")"; }
[ -n "$GOAL" ]       && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="goal=$(rt_url_encode "$GOAL")"; }
[ -n "$HYPOTHESIS" ] && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="hypothesis=$(rt_url_encode "$HYPOTHESIS")"; }
[ -n "$TYP" ]        && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="type=$(rt_url_encode "$TYP")"; }
[ -n "$MOST" ]       && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="most_retrieved=${MOST}"; }
[ -n "$LEAST" ]      && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="least_retrieved=${LEAST}"; }
[ -n "$RECENT" ]     && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="recent=${RECENT}"; }
for key in "${FLAG_KEYS[@]+"${FLAG_KEYS[@]}"}"; do
    [ -n "$QUERY" ] && QUERY+="&"
    QUERY+="${key}=1"
done

if [ -n "$QUERY" ]; then
    rc=0
    rt_call GET /v1/experience/read --query "$QUERY" || rc=$?
else
    echo "Error: at least one filter is required." >&2
    exit 1
fi

case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call GET /v1/experience/read --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "experience-read.sh";;
    *)
        exit $rc;;
esac
