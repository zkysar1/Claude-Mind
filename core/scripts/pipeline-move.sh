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
REC_ID=""
STAGE=""
declare -a PASSTHROUGH=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -*)
            PASSTHROUGH+=("$1"); shift;;
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
BODY=""
if ! [ -t 0 ]; then
    BODY="$(cat)"
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
