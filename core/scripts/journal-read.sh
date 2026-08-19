#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Read journal records — daemon-aware wrapper.
#
# Migrated for Phase B PR 4. Daemon path: rt_call /v1/journal/read.
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

declare -a FLAG_KEYS=()
declare -a PASSTHROUGH=()
SESSION=""
DATE=""
RECENT=""

# Value-arg pattern: "${2-}" + safe shift; see retrieve.sh for rationale.
# (--recent below already checks $#.)
while [[ $# -gt 0 ]]; do
    case "$1" in
        --session)
            SESSION="${2-}"; PASSTHROUGH+=(--session "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --date)
            DATE="${2-}"; PASSTHROUGH+=(--date "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --recent)
            # --recent takes optional N (CLI default 5)
            if [ $# -gt 1 ] && [[ "$2" =~ ^[0-9]+$ ]]; then
                RECENT="$2"; PASSTHROUGH+=("$1" "$2"); shift $(( $# >= 2 ? 2 : 1 ))
            else
                RECENT="5"; PASSTHROUGH+=("$1"); shift
            fi;;
        --summary) FLAG_KEYS+=(summary); PASSTHROUGH+=("$1"); shift;;
        --meta)    FLAG_KEYS+=(meta);    PASSTHROUGH+=("$1"); shift;;
        --latest)  FLAG_KEYS+=(latest);  PASSTHROUGH+=("$1"); shift;;
        *)
            PASSTHROUGH+=("$1"); shift;;
    esac
done

source "$CORE_ROOT/scripts/_runtime.sh"

QUERY=""
[ -n "$SESSION" ] && QUERY="session=${SESSION}"
[ -n "$DATE" ]    && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="date=$(rt_url_encode "$DATE")"; }
[ -n "$RECENT" ]  && { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="recent=${RECENT}"; }
for key in "${FLAG_KEYS[@]+"${FLAG_KEYS[@]}"}"; do
    [ -n "$QUERY" ] && QUERY+="&"
    QUERY+="${key}=1"
done

if [ -z "$QUERY" ]; then
    echo "Error: at least one filter is required (--session, --date, --recent, --summary, --meta, --latest)." >&2
    exit 1
fi
rc=0
rt_call GET /v1/journal/read --query "$QUERY" || rc=$?

case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call GET /v1/journal/read --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "journal-read.sh";;
    *)
        exit $rc;;
esac
