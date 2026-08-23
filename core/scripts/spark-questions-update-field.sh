#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-15 (H2 Wave 3). No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
#   zeta/reports/phase3-h2-wave-plan.md (generic store endpoint)
# spark-questions-update-field -- daemon-aware wrapper. Updates a single
# field on a spark-question record.
#
# Usage:  spark-questions-update-field.sh <id> <field> <value>
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
# STRICT argv (). The previous `-*) shift;;` arm SWALLOWED unknown
# flags, sliding the next argument into <value> and clobbering the record with
# rc=0. Refusals exit 2 SPECIFICALLY — see core/scripts/_argv_strict.sh.
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_argv_strict.sh"

argv_strict_parse "spark-questions-update-field.sh" 3 "$@"
REC_ID="${ARGV_POS[0]:-}"
FIELD="${ARGV_POS[1]:-}"
VALUE="$(argv_strict_resolve_value "spark-questions-update-field.sh" "${ARGV_POS[2]:-}")"

if [ -z "$REC_ID" ] || [ -z "$FIELD" ] || [ -z "$VALUE" ]; then
    argv_strict_usage "spark-questions-update-field.sh" 3
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

QUERY="store=spark-questions&id=$(rt_url_encode "$REC_ID")&field=$(rt_url_encode "$FIELD")&value=$(rt_url_encode "$VALUE")"

rc=0
RESPONSE="$(rt_call POST /v1/store/set-field \
    --query "$QUERY")" || rc=$?

case $rc in
    0) _print_record "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-15 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/store/set-field \
                --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then _print_record "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "spark-questions-update-field.sh";;
    *) exit $rc;;
esac
