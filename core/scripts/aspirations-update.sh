#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# aspirations-update — daemon-aware wrapper (PR 54).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse args (positional asp_id, field, value + --source flag)
#   3. JSON-encode value via py -3 (mirrors aspirations.py parse_value)
#   4. POST /v1/aspirations/update with {field: encoded_value} body
#   5. On 200, print `aspiration` field from response to stdout (matches
#      legacy `json.dumps(asp, indent=2, ensure_ascii=False)`)
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
_ACCEPTED_FLAGS="--source"

# --- Parse args -----------------------------------------------------------
SOURCE_VAL="world"
declare -a PASSTHROUGH=()
declare -a PASSTHROUGH_SOURCE=()
declare -a POSITIONALS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            SOURCE_VAL="${2-}"
            PASSTHROUGH_SOURCE=(--source "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        -h|--help)
            # BEFORE the -*) arm: --help is a `-*` token, and refusing it with
            # exit 2 would be a regression the refusal introduced rather than a
            # defect it fixed (). Help exits 0.
            argv_strict_help "$(basename "$0")" "<asp-id> <field> <value>" \
                "$_ACCEPTED_FLAGS";;
        -*)
            # REFUSE (). This arm silently swallowed the flag into a
            # PASSTHROUGH array with no reader, sliding the NEXT token into
            # POSITIONALS and writing it as the field value with rc=0.
            argv_strict_refuse_unknown "$(basename "$0")" "$1" "$_ACCEPTED_FLAGS";;
        *)
            POSITIONALS+=("$1")
            PASSTHROUGH+=("$1"); shift;;
    esac
done

ASP_ID="${POSITIONALS[0]-}"
FIELD="${POSITIONALS[1]-}"
VALUE="${POSITIONALS[2]-}"

# Missing positionals → error
if [ -z "$ASP_ID" ] || [ -z "$FIELD" ] || [ -z "$VALUE" ]; then
    echo "Error: asp_id, field, and value are all required." >&2
    exit 1
fi

# --- Daemon path ---------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

# Encode value as JSON, mirroring aspirations.py parse_value.
ENCODED_VALUE=$($(rt_python_launcher) -c '
import json, sys
v = sys.argv[1]
if v == "true":
    r = True
elif v == "false":
    r = False
elif v == "null":
    r = None
elif v == "[]":
    r = []
elif v.startswith("{") or v.startswith("["):
    try:
        r = json.loads(v)
    except json.JSONDecodeError:
        r = v
else:
    try:
        r = int(v)
    except ValueError:
        try:
            r = float(v)
        except ValueError:
            r = v
sys.stdout.write(json.dumps(r))
' "$VALUE")

QUERY="asp_id=${ASP_ID}&source=${SOURCE_VAL}"

# Build body: single JSON object {field: value}
BODY=$($(rt_python_launcher) -c "
import json, sys
sys.stdout.write(json.dumps({sys.argv[1]: json.loads(sys.argv[2])}))
" "$FIELD" "$ENCODED_VALUE")

rc=0
RESPONSE="$(rt_call POST /v1/aspirations/update \
    --query "$QUERY" \
    --body-string "$BODY")" || rc=$?

case $rc in
    0)
        # 200: print `aspiration` to stdout (legacy CLI shape).
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
asp = resp.get('aspiration')
if asp is None:
    print(json.dumps(resp, indent=2, ensure_ascii=False))
else:
    print(json.dumps(asp, indent=2, ensure_ascii=False))
"
        exit 0;;
    2)
        printf '%s\n' "$RESPONSE" >&2
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/aspirations/update \
                --query "$QUERY" \
                --body-string "$BODY")" || rc=$?
            if [ "$rc" = "0" ]; then
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
asp = resp.get('aspiration')
if asp is None:
    print(json.dumps(resp, indent=2, ensure_ascii=False))
else:
    print(json.dumps(asp, indent=2, ensure_ascii=False))
"
                exit 0
            fi
        fi
        rt_no_daemon_error "aspirations-update.sh";;
    *)
        exit $rc;;
esac
