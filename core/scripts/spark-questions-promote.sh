#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# spark-questions-promote — daemon-aware wrapper. Promotes a candidate
# spark question to an active question with a new ID.
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse positional args (candidate_id, new_id)
#   3. POST /v1/spark-questions/promote?candidate_id=<id>&new_id=<id>
#   4. On 200, extract .record and print indent=2 ensure_ascii=False
#      (+ print .note to stderr if present, matching CLI stderr parity)
#
# Usage: bash core/scripts/spark-questions-promote.sh <candidate_id> <new_id>
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse positional args ------------------------------------------------
CANDIDATE_ID="${1-}"
NEW_ID="${2-}"

if [ -z "$CANDIDATE_ID" ]; then
    echo "Error: candidate_id is required" >&2
    exit 1
fi
if [ -z "$NEW_ID" ]; then
    echo "Error: new_id is required (sq-NNN format)" >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

_print_record() {
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
rec = resp.get('record') or resp
print(json.dumps(rec, indent=2, ensure_ascii=False))
note = resp.get('note')
if note:
    print(note, file=sys.stderr)
"
}

QUERY="candidate_id=$(rt_url_encode "$CANDIDATE_ID")&new_id=$(rt_url_encode "$NEW_ID")"

rc=0
RESPONSE="$(rt_call POST /v1/spark-questions/promote \
    --query "$QUERY")" || rc=$?

case $rc in
    0) _print_record "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/spark-questions/promote \
                --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then _print_record "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "spark-questions-promote.sh";;
    *) exit $rc;;
esac
