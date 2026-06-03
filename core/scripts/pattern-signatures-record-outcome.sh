#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# pattern-signatures-record-outcome — daemon-aware wrapper. Records a
# pattern match outcome (CONFIRMED or CORRECTED) for a signature record.
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse positional args (rec_id, outcome)
#   3. POST /v1/pattern-signatures/record-outcome?rec_id=<id>&outcome=<outcome>
#   4. On 200, print the record to stdout (indent=2, ensure_ascii=False)
#
# Usage: bash core/scripts/pattern-signatures-record-outcome.sh <rec_id> <outcome>
#   rec_id:  Pattern signature ID (e.g. sig-001)
#   outcome: CONFIRMED or CORRECTED
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse positional args ------------------------------------------------
REC_ID="${1-}"
OUTCOME="${2-}"

if [ -z "$REC_ID" ]; then
    echo "Error: rec_id is required (positional arg 1)" >&2
    exit 1
fi
if [ -z "$OUTCOME" ]; then
    echo "Error: outcome is required (positional arg 2: CONFIRMED or CORRECTED)" >&2
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
"
}

QUERY="rec_id=$(rt_url_encode "$REC_ID")&outcome=$(rt_url_encode "$OUTCOME")"

rc=0
RESPONSE="$(rt_call POST /v1/pattern-signatures/record-outcome \
    --query "$QUERY")" || rc=$?

case $rc in
    0) _print_record "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/pattern-signatures/record-outcome \
                --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then _print_record "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "pattern-signatures-record-outcome.sh";;
    *) exit $rc;;
esac
