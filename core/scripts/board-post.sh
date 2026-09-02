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
CHANNEL=""; AUTHOR=""; MSG_TYPE=""; REPLY_TO=""; TAGS=""; ALLOW_JSON_BODY=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --channel)  CHANNEL="${2-}";  shift $(( $# >= 2 ? 2 : 1 ));;
        --author)   AUTHOR="${2-}";   shift $(( $# >= 2 ? 2 : 1 ));;
        --type)     MSG_TYPE="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --reply-to) REPLY_TO="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --tags)     TAGS="${2-}";     shift $(( $# >= 2 ? 2 : 1 ));;
        # Escape hatch for the rare caller that genuinely means to post a JSON
        # document AS the message body. Deliberately not a silent default: see
        # the json-body guard below.
        --allow-json-body) ALLOW_JSON_BODY=1; shift;;
        # Unknown args are REFUSED, not swallowed. A bare `*) shift;;` silently
        # discarded both the flag and its value, so `--message "<text>"` produced
        # an EMPTY stdin body and the daemon answered the confusing `empty_text`
        # instead of naming the real mistake. guard-1036 / guard-1531
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

# --- Refuse a JSON-object body () -------------------------------
# Measured 2026-08-30 over 56,212 board records: 276 posts across EIGHT agents
# arrived with the whole message body as a JSON object like
# {"subject":"...","text":"...","tags":[...]}. That is not carelessness. Four
# guardrails (guard-2037, guard-2933, guard-1776, guard-2229) correctly teach
# that framework writer scripts take a JSON RECORD ON STDIN -- measured across
# 37 scripts under core/. THIS script is the exception: stdin is the raw message
# TEXT and every other field is a FLAG. So an agent applying the house
# convention lands here, the post SUCCEEDS, returns an id, and stores the JSON
# blob as its literal body with tags:[].
#
# The cost is not cosmetic: --tags is the only way `board-read.sh --tag` can
# find a post, so all 276 are unfindable by tag, permanently. ("subject" is not
# a board field AT ALL -- 0 of 6,026 coordination posts carry one -- so callers
# were also filling a field that has never existed.)
#
# Refuse rather than silently unpack: a refused post is retried correctly in
# seconds, while a mangled one loses its tags forever and nothing surfaces it.
# The 85 whole-JSON bodies that carry none of these keys are intentional payload
# posts and are deliberately NOT matched -- the predicate is anchored on the
# metadata keys, not on "looks like JSON" (guard-2860: never relax a predicate
# into a pattern).
if [ "$ALLOW_JSON_BODY" != "1" ]; then
    _jb_rc=0
    printf '%s' "$BODY" | $(rt_python_launcher) -c '
import json, sys
s = sys.stdin.read().strip()
if not (s.startswith("{") and s.endswith("}")):
    sys.exit(0)
try:
    obj = json.loads(s)
except Exception:
    sys.exit(0)
if not isinstance(obj, dict):
    sys.exit(0)
meta = set(obj.keys()) & {"text", "subject", "body", "message"}
if not meta:
    sys.exit(0)
w = sys.stderr.write
w("board-post.sh: REFUSING -- the body is a JSON object, not message text.\n")
w("\n  WHAT YOU SENT (parsed from your own payload):\n")
for k in ("text", "body", "message"):
    if k in obj:
        w("    %-8s : %d chars  -> this is what should have been on STDIN\n"
          % (k, len(str(obj[k]))))
if "subject" in obj:
    w("    subject  : NOT A BOARD FIELD -- no post has ever had one.\n")
    w("               Make it the first line of your text instead.\n")
if "tags" in obj:
    w("    tags     : %s  -> WOULD BE LOST (tags come from --tags)\n"
      % (obj.get("tags"),))
if "type" in obj:
    w("    type     : %s  -> WOULD BE LOST (type comes from --type)\n"
      % (obj.get("type"),))
w("\n  WHY THIS IS REFUSED RATHER THAN ACCEPTED:\n")
w("    Without this guard the post SUCCEEDS and returns an id, storing the\n")
w("    whole JSON blob as its literal body with tags:[]. board-read.sh --tag\n")
w("    can then never find it. 276 such posts exist across 8 agents.\n")
w("\n  YOU PROBABLY APPLIED THE HOUSE CONVENTION, AND IT DOES NOT HOLD HERE:\n")
w("    Framework writer scripts take a JSON record on STDIN (guard-2037,\n")
w("    guard-2933, guard-1776, guard-2229 -- 37 scripts). board-post.sh is\n")
w("    the exception: STDIN is the raw TEXT, everything else is a flag.\n")
tagstr = ""
if isinstance(obj.get("tags"), list):
    tagstr = " --tags " + ",".join(str(t) for t in obj["tags"])
elif obj.get("tags"):
    tagstr = " --tags " + str(obj["tags"])
typestr = " --type " + str(obj["type"]) if obj.get("type") else ""
w("\n  CORRECT INVOCATION FOR THIS EXACT PAYLOAD:\n")
w("    printf %s \"$YOUR_TEXT\" | bash core/scripts/board-post.sh \\\n")
w("      --channel <ch>%s%s\n" % (tagstr, typestr))
w("\n    (--allow-json-body overrides, ONLY if you truly mean to post a JSON\n")
w("     document as the message body.)\n")
sys.exit(1)
' || _jb_rc=$?
    # rc captured on the LAST pipeline element, which IS the python (guard-1150).
    if [ "$_jb_rc" -ne 0 ]; then exit 1; fi
fi

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
