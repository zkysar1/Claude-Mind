#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# pipeline-move — daemon-aware wrapper (PR 8).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse args + read stdin merge data
#   3. POST /v1/pipeline/move with rec_id + stage as query params
#   4. On 200, print the record to stdout
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
# Sourced BEFORE _runtime.sh so the refusal cannot be masked by a daemon
# failure (see _argv_strict.sh header). One file read on the hot path.
source "$CORE_ROOT/scripts/_argv_strict.sh"

REC_ID=""
STAGE=""
# This wrapper takes NO flags — only two positionals. Held in one variable so
# the help text and the refusal message cannot drift apart (_argv_strict.sh
# header: never two literals).
_ACCEPTED_FLAGS="(none — this wrapper takes two positionals and no flags)"
declare -a PASSTHROUGH=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            argv_strict_help "pipeline-move.sh" "<rec-id> <stage>" "$_ACCEPTED_FLAGS";;
        -*)
            # REFUSE (). This arm used to be
            # `PASSTHROUGH+=("$1"); shift` — and PASSTHROUGH has NO READER in
            # this file, a vestige of the Python CLI fallback deleted
            # 2026-05-14 (.claude/rules/no-python-cli-fallback.md). So an
            # unrecognised flag was appended to an array nobody reads and
            # vanished; the flag's VALUE then fell through to the positional
            # arm below and became rec_id or stage. A pipeline record moved to
            # the wrong stage, exit 0.
            argv_strict_refuse_unknown "pipeline-move.sh" "$1" "$_ACCEPTED_FLAGS";;
        *)
            # First positional = rec_id, second = stage
            if [ -z "$REC_ID" ]; then
                REC_ID="$1"
            elif [ -z "$STAGE" ]; then
                STAGE="$1"
            fi
            PASSTHROUGH+=("$1"); shift;;
    esac
done

# Need both rec_id and stage.
if [ -z "$REC_ID" ] || [ -z "$STAGE" ]; then
    echo "Error: both rec_id and stage are required." >&2
    exit 1
fi

# Read stdin (optional merge data) BEFORE invoking the daemon.
# Guarded (, guard-664 bash twin): non-tty stdin does NOT guarantee
# EOF — a backgrounded Bash task inherits an open, never-closing stdin, and a
# bare `cat` wedges the wrapper forever (4 zombie process trees found
# 2026-07-16; killed with exit 144, their stage moves never landed). Probe the
# FIRST line with a bounded timeout: real piped callers (`echo '<json>' | ...`)
# have data in the pipe buffer at exec so the timeout never fires for them; an
# idle inherited descriptor times out and degrades to no-merge-data with a
# loud stderr note. `|| [ -n "$first_chunk" ]` keeps single-line input that
# lacks a trailing newline (read exits nonzero on EOF but fills the var).
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
        echo "pipeline-move.sh: stdin open but idle after 2s — proceeding without merge data (backgrounded-task guard, g-115-2291)" >&2
    fi
    # rc_read == 1 with empty var (immediate EOF, e.g. </dev/null): silent, no merge data.
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="id=$(rt_url_encode "$REC_ID")&stage=$(rt_url_encode "$STAGE")"

if [ -z "$BODY" ]; then BODY='{}'; fi

rc=0
RESPONSE="$(rt_call POST /v1/pipeline/move \
    --query "$QUERY" \
    --body-string "$BODY" \
    )" || rc=$?

case $rc in
    0)
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
rec = resp.get('record') or resp
print(json.dumps(rec, indent=2, ensure_ascii=False))
"
        exit 0;;
    2)
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/pipeline/move \
                --query "$QUERY" \
                --body-string "$BODY")" || rc=$?
            if [ "$rc" = "0" ]; then
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
rec = resp.get('record') or resp
print(json.dumps(rec, indent=2, ensure_ascii=False))
"
                exit 0
            fi
        fi
        rt_no_daemon_error "pipeline-move.sh";;
    *)
        exit $rc;;
esac
