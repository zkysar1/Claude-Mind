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
        # Unknown args are REFUSED, not swallowed. A bare `*) shift;;` silently
        # discarded both the flag and its value, so `--message "<text>"` produced
        # an EMPTY stdin body and the daemon answered the confusing `empty_text`
        # instead of naming the real mistake. guard-1036 / guard-1394 / guard-1531
        # all already forbade that call shape and it still recurred 4x, because a
        # guardrail cannot fire at the moment a flag is typed -- the parser can.
        *)
            echo "Error: unknown argument '$1'" >&2
            echo "  The MESSAGE TEXT is read from STDIN. There is no --message/--body/--text flag." >&2
            echo "  Correct: echo \"msg\" | bash core/scripts/board-post.sh --channel <ch> [--type <t>] [--tags <a,b>] [--author <a>] [--reply-to <id>]" >&2
            exit 1
            ;;
    esac
done

if [ -z "$CHANNEL" ]; then
    echo "Error: --channel is required" >&2
    exit 1
fi

# Read stdin (the message text) BEFORE sourcing _runtime.sh.
# Guarded (, porting the  bounded read from pipeline-move.sh;
# guard-3393 door (b)): a bare `cat` wedges FOREVER when stdin is open but never
# delivers EOF — any backgrounded invocation inherits a live descriptor. Observed
# 2026-07-26: a backgrounded post sat 25 minutes in state S, wrote nothing, and had
# to be killed by PID; nothing timed out and nothing logged. `[ -t 0 ]` CANNOT
# detect this (measured FALSE for both /dev/null and a never-EOF socket stdin), so
# the tty test only skips the interactive case — the bounded probe is what closes
# the door. Real piped callers (`echo "msg" | ...`) have data in the pipe buffer at
# exec, so the timeout never fires for them. `|| [ -n "$first_chunk" ]` keeps
# single-line input lacking a trailing newline (read exits nonzero on EOF but fills
# the var). UNLIKE pipeline-move.sh, the body here IS the message, so an idle stdin
# is a FATAL usage error, not a degrade: a post with an empty body must error, never
# block and never post empty.
BODY=""
if ! [ -t 0 ]; then
    first_chunk=""
    rc_read=0
    IFS= read -r -t 2 first_chunk || rc_read=$?
    if [ "$rc_read" -eq 0 ] || [ -n "$first_chunk" ]; then
        rest="$(cat)"
        if [ -n "$rest" ]; then
            BODY="$first_chunk"$'\n'"$rest"
        else
            BODY="$first_chunk"
        fi
    elif [ "$rc_read" -gt 128 ]; then
        echo "board-post.sh: stdin open but idle after 2s — refusing to post an empty message (backgrounded-task guard, g-115-3284/g-115-2291)." >&2
        echo "  The MESSAGE TEXT is read from STDIN. There is no --message/--body/--text flag." >&2
        echo "  Correct: echo \"msg\" | bash core/scripts/board-post.sh --channel <ch> [--type <t>] [--tags <a,b>]" >&2
        echo "  If calling from a backgrounded task, redirect stdin explicitly: ... | bash core/scripts/board-post.sh --channel <ch>" >&2
        exit 1
    fi
    # rc_read == 1 with empty var (immediate EOF, e.g. </dev/null): falls through to
    # the empty-body check below, which reports the usage error.
fi

if [ -z "$BODY" ]; then
    echo "Error: empty message body — nothing to post." >&2
    echo "  The MESSAGE TEXT is read from STDIN. There is no --message/--body/--text flag." >&2
    echo "  Correct: echo \"msg\" | bash core/scripts/board-post.sh --channel <ch> [--type <t>] [--tags <a,b>]" >&2
    exit 1
fi

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
# : any advisory warnings the daemon returns (e.g. a dangling
# reply_to) go to STDERR, keeping stdout = just the id so id-parsing callers
# are unaffected.
_extract_id() {
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
print(resp['id'])
for w in (resp.get('warnings') or []):
    print('[board-post] WARN: ' + str(w), file=sys.stderr)
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
