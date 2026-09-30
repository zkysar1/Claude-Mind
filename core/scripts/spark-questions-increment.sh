#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# spark-questions-increment — daemon-aware wrapper. Atomically increments
# a counter (times_asked or sparks_generated) and recomputes yield_rate.
# Several <rec_id> <field> pairs in ONE call are ONE locked rewrite of the
# store, via POST /v1/spark-questions/increment-batch (): a spark
# phase that bumps ~23 counters one call at a time rewrites the file ~23 times.
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse positional args (rec_id, field [, rec_id, field ...])
#   3. One pair:   POST /v1/spark-questions/increment?rec_id=<id>&field=<field>
#      More pairs: POST /v1/spark-questions/increment-batch, JSON body
#   4. On 200, print .record (one pair) or .records (more) indent=2 ensure_ascii=False
#
# Usage: bash core/scripts/spark-questions-increment.sh <rec_id> <field> [<rec_id> <field> ...]
#   field: times_asked | sparks_generated
#   With more than one pair, an entry whose record is missing or is not a
#   question is skipped and named on stderr (exit 1); the other entries apply.
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse positional args ------------------------------------------------
REC_ID="${1-}"
FIELD="${2-}"

if [ -z "$REC_ID" ]; then
    echo "Error: rec_id is required" >&2
    exit 1
fi
if [ -z "$FIELD" ]; then
    echo "Error: field is required (times_asked or sparks_generated)" >&2
    exit 1
fi
if [ $(( $# % 2 )) -ne 0 ]; then
    echo "Error: arguments must be <rec_id> <field> pairs, got $# arguments" >&2
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

if [ "$#" -gt 2 ]; then
    _print_batch() {
        # shellcheck disable=SC2086
        printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
print(json.dumps(resp.get('records') or [], indent=2, ensure_ascii=False))
skipped = resp.get('skipped') or []
for s in skipped:
    print('Error: skipped %s %s: %s' % (s.get('rec_id'), s.get('field'), s.get('reason')),
          file=sys.stderr)
sys.exit(1 if skipped else 0)
"
    }

    # shellcheck disable=SC2086
    BODY="$($(rt_python_launcher) -c '
import json, sys
a = sys.argv[1:]
print(json.dumps({"increments": [{"rec_id": a[i], "field": a[i + 1]}
                                 for i in range(0, len(a), 2)]}))
' "$@")"

    rc=0
    RESPONSE="$(rt_call POST /v1/spark-questions/increment-batch \
        --body-string "$BODY")" || rc=$?

    case $rc in
        0) _print_batch "$RESPONSE" || exit 1; exit 0;;
        2) exit 1;;
        3)
            # DAEMON-ONLY: no Python CLI fallback (as below).
            if rt_try_autospawn; then
                rc=0
                RESPONSE="$(rt_call POST /v1/spark-questions/increment-batch \
                    --body-string "$BODY")" || rc=$?
                if [ "$rc" = "0" ]; then _print_batch "$RESPONSE" || exit 1; exit 0; fi
            fi
            rt_no_daemon_error "spark-questions-increment.sh";;
        *) exit $rc;;
    esac
fi

QUERY="rec_id=$(rt_url_encode "$REC_ID")&field=$(rt_url_encode "$FIELD")"

rc=0
RESPONSE="$(rt_call POST /v1/spark-questions/increment \
    --query "$QUERY")" || rc=$?

case $rc in
    0) _print_record "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/spark-questions/increment \
                --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then _print_record "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "spark-questions-increment.sh";;
    *) exit $rc;;
esac
