#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# experience-update-field — daemon-aware wrapper. Updates a single field
# on an experience record (live or archive).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse positional args: rec_id field value
#   3. POST /v1/experience/update-field?id=&field=&value=
#   4. On 200, extract .record from JSON response and print (indent=2)
#
# Usage: bash core/scripts/experience-update-field.sh <rec_id> <field> <value>
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
# STRICT (). This was the LAST unguarded member of the six
# <id> <field> <value> siblings: it read $1/$2/$3 blindly, so
# `<id> <field> --value-file <path>` stored the literal string "--value-file"
# as the field value with rc=0 — the write-side swallow that clobbered
# guard-1615 on the sibling wrappers (). Same two rules as the four
# guarded siblings, enforced BEFORE _runtime.sh so the refusal is cheap and
# cannot be masked by a daemon failure: unknown leading-dash argument is an
# ERROR (exit 2), a 4th positional is an ERROR (exit 2). Also gains
# --value-file/--value-stdin support, the sanctioned long-value form.
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_argv_strict.sh"

argv_strict_parse "experience-update-field.sh" 3 "$@"
REC_ID="${ARGV_POS[0]:-}"
FIELD="${ARGV_POS[1]:-}"
VALUE="$(argv_strict_resolve_value "experience-update-field.sh" "${ARGV_POS[2]:-}")"

if [ -z "$REC_ID" ] || [ -z "$FIELD" ] || [ -z "$VALUE" ]; then
    argv_strict_usage "experience-update-field.sh" 3
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

QUERY="id=$(rt_url_encode "$REC_ID")&field=$(rt_url_encode "$FIELD")&value=$(rt_url_encode "$VALUE")"

rc=0
RESPONSE="$(rt_call POST /v1/experience/update-field --query "$QUERY")" || rc=$?

case $rc in
    0) _print_record "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/experience/update-field --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then _print_record "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "experience-update-field.sh";;
    *) exit $rc;;
esac
