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

# : shared strict-argv refusal helpers (uniform message contract).
# Sourced BEFORE _runtime.sh so a refusal cannot be masked by a daemon failure.
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_argv_strict.sh"

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
        -h|--help)
            echo "Usage: board-read.sh --channel <ch> [--since <w>] [--author <a>] [--tag <t>] [--type <t>] [--last <N>] [--json] [--mark-read] [--unread-only]"
            exit 0;;
        --channel) CHANNEL="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --since)   SINCE="${2-}";   shift $(( $# >= 2 ? 2 : 1 ));;
        --author)  AUTHOR="${2-}";  shift $(( $# >= 2 ? 2 : 1 ));;
        --tag)     TAG="${2-}";     shift $(( $# >= 2 ? 2 : 1 ));;
        --type)    TYP="${2-}";    shift $(( $# >= 2 ? 2 : 1 ));;
        --last)    LAST="${2-}";    shift $(( $# >= 2 ? 2 : 1 ));;
        --json)       AS_JSON=1; shift;;
        --mark-read)  MARK_READ=1; shift;;
        --unread-only) UNREAD_ONLY=1; shift;;
        *)
            # : this arm silently appended to a dead PASSTHROUGH
            # accumulator (fed the pre-2026-05-14 CLI fallback, read by nothing
            # since), so a mistyped filter vanished and the call answered the
            # WRONG population with rc=0 (the rb-245 authoritative-false-count
            # shape). Refuse loudly. Exit 2 per the _argv_strict.sh convention
            # (the daemon path exits 1, so tests need a distinct rc).
            argv_strict_refuse_unknown "board-read.sh" "$1" "--channel <ch> | --since <window> | --author <a> | --tag <t> | --type <t> | --last <N> | --json | --mark-read | --unread-only";;
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
