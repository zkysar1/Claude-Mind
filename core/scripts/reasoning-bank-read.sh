#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Read reasoning-bank records — daemon-aware wrapper.
#
# Migrated for Phase B PR 4. Daemon path: rt_call /v1/rb/read.
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

declare -a FLAG_KEYS=()
declare -a PASSTHROUGH=()
REC_ID=""
CATEGORY=""
TAG=""
RECENT=""

# Value-arg pattern: "${2-}" + safe shift; see retrieve.sh for rationale.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --id)
            REC_ID="${2-}"; PASSTHROUGH+=(--id "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --category)
            CATEGORY="${2-}"; PASSTHROUGH+=(--category "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --tag)
            TAG="${2-}"; PASSTHROUGH+=(--tag "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --recent)
            # --recent may be bare (CLI default 10) or take an integer.
            if [ $# -gt 1 ] && [[ "$2" =~ ^[0-9]+$ ]]; then
                RECENT="$2"; PASSTHROUGH+=("$1" "$2"); shift 2
            else
                RECENT="10"; PASSTHROUGH+=("$1"); shift
            fi;;
        --active)      FLAG_KEYS+=(active);    PASSTHROUGH+=("$1"); shift;;
        --universal)   FLAG_KEYS+=(universal); PASSTHROUGH+=("$1"); shift;;
        --summary)     FLAG_KEYS+=(summary);   PASSTHROUGH+=("$1"); shift;;
        *)
            PASSTHROUGH+=("$1"); shift;;
    esac
done

source "$CORE_ROOT/scripts/_runtime.sh"

QUERY=""
[ -n "$REC_ID" ]   && QUERY="id=$(rt_url_encode "$REC_ID")"
[ -n "$CATEGORY" ] && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="category=$(rt_url_encode "$CATEGORY")"; }
[ -n "$TAG" ]      && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="tag=$(rt_url_encode "$TAG")"; }
[ -n "$RECENT" ]   && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="recent=${RECENT}"; }
for key in "${FLAG_KEYS[@]+"${FLAG_KEYS[@]}"}"; do
    [ -n "$QUERY" ] && QUERY+="&"
    QUERY+="${key}=1"
done

if [ -z "$QUERY" ]; then
    echo "Error: at least one filter is required." >&2
    exit 1
fi
rc=0
rt_call GET /v1/rb/read --query "$QUERY" || rc=$?

case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call GET /v1/rb/read --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "reasoning-bank-read.sh";;
    *)
        exit $rc;;
esac
