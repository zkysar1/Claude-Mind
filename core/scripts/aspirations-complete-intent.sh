#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# aspirations-complete-intent.sh — daemon-aware wrapper (PR 51).
#
# Close an aspiration via the intent-satisfaction pathway. Requires JSON
# on stdin: {evidence_goal_ids, rationale, superseded_goal_ids}.
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse --source / positional asp_id
#   3. Read stdin into BODY
#   4. POST /v1/aspirations/complete-intent with body
#   5. On 200, print aspiration JSON to stdout
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
SOURCE_VAL="world"
declare -a POSITIONAL=()
declare -a PASSTHROUGH_SOURCE=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            SOURCE_VAL="${2-}"
            PASSTHROUGH_SOURCE=(--source "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        *)
            POSITIONAL+=("$1"); shift;;
    esac
done
set -- ${POSITIONAL[@]+"${POSITIONAL[@]}"}

ASP_ID="${1:-}"

# --- Read stdin into BODY -------------------------------------------------
# Guarded (1, guard-664 bash twin — same class as pipeline-move.sh):
# non-tty stdin does not guarantee EOF; a backgrounded task inherits an open,
# never-closing stdin and a bare `cat` wedges forever. Bounded first-line
# probe: piped callers deliver instantly; an idle descriptor degrades to
# empty BODY after 2s with a loud note.
BODY=""
if [ ! -t 0 ]; then
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
        echo "aspirations-complete-intent.sh: stdin open but idle after 2s — proceeding without stdin body (backgrounded-task guard, g-115-2291)" >&2
    fi
fi

# Missing asp_id -> error
if [ -z "$ASP_ID" ]; then
    echo "Error: asp_id is required." >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="asp_id=$(rt_url_encode "$ASP_ID")&source=$(rt_url_encode "$SOURCE_VAL")"

rc=0
RESPONSE="$(rt_call POST /v1/aspirations/complete-intent \
    --query "$QUERY" \
    --body-string "$BODY")" || rc=$?

case $rc in
    0)
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
asp = resp.get('aspiration')
if asp is None:
    print(json.dumps(resp, indent=2, ensure_ascii=False))
else:
    print(json.dumps(asp, indent=2, ensure_ascii=False))
"
        exit 0;;
    2)
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/aspirations/complete-intent \
                --query "$QUERY" \
                --body-string "$BODY")" || rc=$?
            if [ "$rc" = "0" ]; then
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
asp = resp.get('aspiration')
if asp is None:
    print(json.dumps(resp, indent=2, ensure_ascii=False))
else:
    print(json.dumps(asp, indent=2, ensure_ascii=False))
"
                exit 0
            fi
        fi
        rt_no_daemon_error "aspirations-complete-intent.sh";;
    *)
        exit $rc;;
esac
