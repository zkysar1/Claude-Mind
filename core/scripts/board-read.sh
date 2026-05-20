#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Read messages from a board channel — daemon-aware wrapper.
# Usage: bash core/scripts/board-read.sh --channel <name> [--since <duration>] \
#                                        [--author <name>] [--type <type>] \
#                                        [--tag <tag>] [--last <N>] [--json]
#
# Migrated for Phase B PR 4. Daemon path: rt_call /v1/board/read.
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

declare -a PASSTHROUGH=()
CHANNEL=""
SINCE=""
AUTHOR=""
TAG=""
TYP=""
LAST=""
AS_JSON=0
MARK_READ=0
UNREAD_ONLY=0

# Value-arg pattern: "${2-}" + safe shift; see retrieve.sh for rationale.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --channel) CHANNEL="${2-}"; PASSTHROUGH+=(--channel "${2-}"); shift $(( $# >= 2 ? 2 : 1 ));;
        --since)   SINCE="${2-}";   PASSTHROUGH+=(--since "${2-}");   shift $(( $# >= 2 ? 2 : 1 ));;
        --author)  AUTHOR="${2-}";  PASSTHROUGH+=(--author "${2-}");  shift $(( $# >= 2 ? 2 : 1 ));;
        --tag)     TAG="${2-}";     PASSTHROUGH+=(--tag "${2-}");     shift $(( $# >= 2 ? 2 : 1 ));;
        --type)    TYP="${2-}";     PASSTHROUGH+=(--type "${2-}");    shift $(( $# >= 2 ? 2 : 1 ));;
        --last)    LAST="${2-}";    PASSTHROUGH+=(--last "${2-}");    shift $(( $# >= 2 ? 2 : 1 ));;
        --json)       AS_JSON=1;      PASSTHROUGH+=("$1"); shift;;
        --mark-read)  MARK_READ=1;    PASSTHROUGH+=("$1"); shift;;
        --unread-only) UNREAD_ONLY=1; PASSTHROUGH+=("$1"); shift;;
        *)
            PASSTHROUGH+=("$1"); shift;;
    esac
done

source "$CORE_ROOT/scripts/_runtime.sh"

if [ -z "$CHANNEL" ]; then
    echo "Error: --channel is required." >&2
    exit 1
else
    QUERY="channel=$(rt_url_encode "$CHANNEL")"
    [ -n "$SINCE" ]  && QUERY+="&since=$(rt_url_encode "$SINCE")"
    [ -n "$AUTHOR" ] && QUERY+="&author=$(rt_url_encode "$AUTHOR")"
    [ -n "$TAG" ]    && QUERY+="&tag=$(rt_url_encode "$TAG")"
    [ -n "$TYP" ]    && QUERY+="&type=$(rt_url_encode "$TYP")"
    [ -n "$LAST" ]   && QUERY+="&last=${LAST}"
    [ "$AS_JSON" = "1" ]     && QUERY+="&json=1"
    [ "$MARK_READ" = "1" ]   && QUERY+="&mark_read=1"
    [ "$UNREAD_ONLY" = "1" ] && QUERY+="&unread_only=1"
    rc=0
    rt_call GET /v1/board/read --query "$QUERY" || rc=$?
fi

case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            rt_call GET /v1/board/read --query "$QUERY" || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "board-read.sh";;
    *)
        exit $rc;;
esac
