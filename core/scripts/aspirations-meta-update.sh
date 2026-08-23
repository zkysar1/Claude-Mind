#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# aspirations-meta-update — daemon-aware wrapper (PR 52).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse --source flag + positional field value args
#   3. POST /v1/aspirations/meta-update?source=<s> with JSON body {field: value}
#   4. On 200, print the updated data to stdout (matches legacy CLI json.dumps shape)
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
# Sourced BEFORE _runtime.sh so the refusal cannot be masked by a daemon
# failure (see _argv_strict.sh header).
source "$CORE_ROOT/scripts/_argv_strict.sh"

SOURCE_VAL="world"
FIELD=""
VALUE=""
# ONE literal, shared by the help text and the refusal message — never two.
_ACCEPTED_FLAGS="--source <world|agent>"
declare -a PASSTHROUGH=()
declare -a PASSTHROUGH_SOURCE=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            SOURCE_VAL="${2-}"
            PASSTHROUGH_SOURCE=(--source "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        -h|--help)
            argv_strict_help "aspirations-meta-update.sh" "[--source world|agent] <field> <value>" "$_ACCEPTED_FLAGS";;
        -*)
            # REFUSE (). "passthrough for argparse on fallback" named
            # a fallback deleted 2026-05-14; PASSTHROUGH has had no reader since.
            # The swallowed flag's VALUE slid into the `*)` arm below and became
            # FIELD (or VALUE), so this wrote the WRONG META FIELD — on the
            # counter store the loop reads every iteration — and exited 0.
            argv_strict_refuse_unknown "aspirations-meta-update.sh" "$1" "$_ACCEPTED_FLAGS";;
        *)
            # Positional: first is field, second is value
            if [ -z "$FIELD" ]; then
                FIELD="$1"
            elif [ -z "$VALUE" ]; then
                VALUE="$1"
            fi
            PASSTHROUGH+=("$1"); shift;;
    esac
done

# Missing field or value → error
if [ -z "$FIELD" ] || [ -z "$VALUE" ]; then
    echo "Error: both field and value are required." >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

# Build the JSON body. parse_value in the CLI handles type coercion (true →
# boolean, integers, JSON objects). The daemon body is raw JSON, so we use a
# small inline python to coerce the value string the same way cmd_meta_update
# does (via parse_value).
BODY="$($(rt_python_launcher) -c "
import json, sys
field, val_str = sys.argv[1], sys.argv[2]
# Mirror aspirations.py::parse_value
if val_str == 'true': val = True
elif val_str == 'false': val = False
elif val_str == 'null': val = None
elif val_str == '[]': val = []
elif val_str.startswith('{') or val_str.startswith('['):
    try: val = json.loads(val_str)
    except json.JSONDecodeError: val = val_str
else:
    try: val = int(val_str)
    except ValueError:
        try: val = float(val_str)
        except ValueError: val = val_str
print(json.dumps({field: val}))
" "$FIELD" "$VALUE")"

QUERY="source=$(rt_url_encode "$SOURCE_VAL")"

rc=0
RESPONSE="$(rt_call POST /v1/aspirations/meta-update \
    --query "$QUERY" \
    --body-string "$BODY")" || rc=$?

case $rc in
    0)
        # 200: extract .data and pretty-print (matches legacy CLI output shape).
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
data = resp.get('data', resp)
print(json.dumps(data, indent=2, ensure_ascii=False))
"
        exit 0;;
    2)
        # Daemon answered 4xx/5xx; body already written to stderr by rt_curl.
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/aspirations/meta-update \
                --query "$QUERY" \
                --body-string "$BODY")" || rc=$?
            if [ "$rc" = "0" ]; then
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
data = resp.get('data', resp)
print(json.dumps(data, indent=2, ensure_ascii=False))
"
                exit 0
            fi
        fi
        rt_no_daemon_error "aspirations-meta-update.sh";;
    *)
        exit $rc;;
esac
