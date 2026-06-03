#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Post a message to a board channel. Message text is read from stdin.
# Daemon path: rt_call POST /v1/board/post.
# The endpoint returns JSON {"ok":true,"id":"msg-...","record":{...}};
# the OLD CLI printed only the message ID, so _extract_id reproduces that.
# Usage: echo "message" | bash core/scripts/board-post.sh --channel <name> [--author <a>] [--type <t>] [--reply-to <id>] [--tags <t1,t2>]
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
CHANNEL=""; AUTHOR=""; MSG_TYPE=""; REPLY_TO=""; TAGS=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --channel)  CHANNEL="${2-}";  shift $(( $# >= 2 ? 2 : 1 ));;
        --author)   AUTHOR="${2-}";   shift $(( $# >= 2 ? 2 : 1 ));;
        --type)     MSG_TYPE="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --reply-to) REPLY_TO="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --tags)     TAGS="${2-}";     shift $(( $# >= 2 ? 2 : 1 ));;
        *) shift;;
    esac
done

if [ -z "$CHANNEL" ]; then
    echo "Error: --channel is required" >&2
    exit 1
fi

# Read stdin (the message text) BEFORE sourcing _runtime.sh.
BODY="$(cat)"

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

# Build query string -------------------------------------------------------
QUERY="channel=$(rt_url_encode "$CHANNEL")"
[ -n "$AUTHOR" ]   && QUERY+="&author=$(rt_url_encode "$AUTHOR")"
[ -n "$MSG_TYPE" ] && QUERY+="&type=$(rt_url_encode "$MSG_TYPE")"
[ -n "$REPLY_TO" ] && QUERY+="&reply_to=$(rt_url_encode "$REPLY_TO")"
[ -n "$TAGS" ]     && QUERY+="&tags=$(rt_url_encode "$TAGS")"

# Translate daemon JSON to CLI-compat stdout: print only the message ID.
_extract_id() {
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
print(resp['id'])
"
}

rc=0
RESPONSE="$(rt_call POST /v1/board/post \
    --query "$QUERY" \
    --body-string "$BODY")" || rc=$?

case $rc in
    0) _extract_id "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/board/post \
                --query "$QUERY" \
                --body-string "$BODY")" || rc=$?
            if [ "$rc" = "0" ]; then _extract_id "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "board-post.sh";;
    *) exit $rc;;
esac
