#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-15 (H2 Wave 3). No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
#   zeta/reports/phase3-h2-wave-plan.md (generic store endpoint)
# spark-questions-add -- daemon-aware wrapper. Appends a spark-question
# record from stdin JSON (handles both question and candidate types).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Read stdin body (JSON spark-question record)
#   3. POST /v1/store/append?store=spark-questions
#   4. On 200, print the record to stdout (indent=2, ensure_ascii=False)
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
SCHEMA=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --schema) SCHEMA=1; shift;;
        --help|-h)
            # A stdin-body reader HANGS on --help without this branch (guard-3145).
            echo "Usage: bash $0 < record.json   — the record is JSON on STDIN; there are NO field flags." >&2
            echo "Canonical form: core/config/conventions/stdin-json-inputs.md" >&2
            exit 0;;
        *)
            # Was `*) shift;;` — silently discarded flags, then blocked forever in
            # BODY="$(cat)" wherever stdin never delivers EOF ().
            echo "Error: '$1' is not a CLI flag for this script — the record goes in the JSON body via stdin." >&2
            echo "Run: bash $0 --help" >&2
            exit 2;;
    esac
done

if [ "$SCHEMA" = "1" ]; then
    echo "Error: --schema is no longer available. See mind_api/src/endpoints/store.py + mind_api/src/store_registry.py for the spark-questions record contract." >&2
    exit 1
fi

# Read stdin (the JSON spark-question record) BEFORE invoking the daemon.
BODY="$(cat)"

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

rc=0
RESPONSE="$(rt_call POST /v1/store/append \
    --query "store=spark-questions" \
    --body-string "$BODY")" || rc=$?

case $rc in
    0) _print_record "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-15 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/store/append \
                --query "store=spark-questions" \
                --body-string "$BODY")" || rc=$?
            if [ "$rc" = "0" ]; then _print_record "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "spark-questions-add.sh";;
    *) exit $rc;;
esac
