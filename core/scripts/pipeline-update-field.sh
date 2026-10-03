#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# pipeline-update-field — daemon-aware wrapper (PR 8).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse rec_id and field, and the value: positional, --value-file or --value-stdin
#   3. POST /v1/pipeline/update-field with query params
#   4. On 200, print the record to stdout
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# Shared unknown-flag refusal (). Sourced BEFORE _runtime.sh so the
# refusal is cheap and cannot be masked by a daemon failure.
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_argv_strict.sh"
# ONE literal, referenced by BOTH the --help arm and the refusal (
# fresh-eyes F-002). These were two copies until the review: the helper's own
# comment asserted they came from one, which was simply false, and two strings
# that must agree are the drift surface the refusal exists to remove.
_ACCEPTED_FLAGS="--value-file <path> | --value-stdin"

# --- Parse args -----------------------------------------------------------
REC_ID=""
FIELD=""
VALUE=""
# Read by argv_strict_resolve_value below. This parser sets them itself because
# it does not call argv_strict_parse.
ARGV_VALUE_FILE=""
ARGV_VALUE_STDIN=0
declare -a PASSTHROUGH=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            # BEFORE the -*) arm: --help is a `-*` token, and refusing it with
            # exit 2 would be a regression the refusal introduced rather than a
            # defect it fixed (). Help exits 0.
            argv_strict_help "$(basename "$0")" \
                "<rec-id> <field> (<value> | --value-file <path> | --value-stdin)" \
                "$_ACCEPTED_FLAGS";;
        # The value can come from a file or stdin instead of argv ().
        # On Windows, Git bash cuts an argv word holding whitespace at 8,186
        # characters with rc=0, so a long narrative value needs one of these.
        --value-file)
            if [ $# -lt 2 ]; then
                printf '%s: --value-file requires a path\n' "$(basename "$0")" >&2
                exit 2
            fi
            ARGV_VALUE_FILE="$2"; shift 2;;
        --value-file=*)
            ARGV_VALUE_FILE="${1#--value-file=}"; shift;;
        --value-stdin)
            ARGV_VALUE_STDIN=1; shift;;
        -*)
            # REFUSE (). This wrapper is named identically to the four
            # *-update-field siblings that DID adopt the strict parser, so it read
            # as converted at a glance while still swallowing unknown flags into a
            # PASSTHROUGH array nothing reads — sliding the next token into VALUE.
            argv_strict_refuse_unknown "$(basename "$0")" "$1" "$_ACCEPTED_FLAGS";;
        *)
            if [ -z "$REC_ID" ]; then
                REC_ID="$1"
            elif [ -z "$FIELD" ]; then
                FIELD="$1"
            elif [ -z "$VALUE" ]; then
                VALUE="$1"
            fi
            PASSTHROUGH+=("$1"); shift;;
    esac
done

# One value source only. Two at once exit 2 inside the resolver, before any
# write. Like the sibling wrappers, it drops trailing newlines from the value.
VALUE="$(argv_strict_resolve_value "$(basename "$0")" "$VALUE")"

# Need the record id, the field and a value.
if [ -z "$REC_ID" ] || [ -z "$FIELD" ] || [ -z "$VALUE" ]; then
    echo "Error: rec_id, field, and value are all required." >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="id=$(rt_url_encode "$REC_ID")&field=$(rt_url_encode "$FIELD")&value=$(rt_url_encode "$VALUE")"

rc=0
RESPONSE="$(rt_call POST /v1/pipeline/update-field \
    --query "$QUERY" \
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
            RESPONSE="$(rt_call POST /v1/pipeline/update-field \
                --query "$QUERY")" || rc=$?
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
        rt_no_daemon_error "pipeline-update-field.sh";;
    *)
        exit $rc;;
esac
