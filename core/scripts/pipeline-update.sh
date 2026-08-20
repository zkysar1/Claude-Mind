#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# pipeline-update — daemon-aware wrapper (PR 55).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse positional arg: rec_id
#   3. Read stdin body (JSON replacement record)
#   4. POST /v1/pipeline/update?id=<rec_id>
#   5. On 200, print the record to stdout
#
# When daemon is unreachable, auto-spawns and retries once; fails loud.
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# : shared strict-argv refusal helpers (uniform message contract).
# Sourced BEFORE _runtime.sh so a refusal cannot be masked by a daemon failure.
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_argv_strict.sh"

# --- Parse args -----------------------------------------------------------
REC_ID=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            echo "Usage: printf '%s' '<json>' | pipeline-update.sh <rec_id>"
            exit 0;;
        -*)
            # : was a silent append to the dead PASSTHROUGH
            # accumulator — a mistyped flag vanished with rc=0, and the update
            # JSON arrives on STDIN, so a flag-only invocation then blocked on
            # the stdin read. Refuse loudly, before the cat.
            argv_strict_refuse_unknown "pipeline-update.sh" "$1" "(none — the update JSON arrives on STDIN; takes <rec_id>)";;
        *)
            if [ -z "$REC_ID" ]; then
                REC_ID="$1"
            else
                argv_strict_refuse_extra_positional "pipeline-update.sh" "$1" 1 ""
            fi
            shift;;
    esac
done

# Need rec_id for the daemon path.
if [ -z "$REC_ID" ]; then
    echo "Error: rec_id is required." >&2
    exit 1
fi

# Read stdin (the JSON replacement record) BEFORE invoking the daemon.
BODY="$(cat)"

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="id=$(rt_url_encode "$REC_ID")"

rc=0
RESPONSE="$(rt_call POST /v1/pipeline/update \
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
            RESPONSE="$(rt_call POST /v1/pipeline/update \
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
        rt_no_daemon_error "pipeline-update.sh";;
    *)
        exit $rc;;
esac
