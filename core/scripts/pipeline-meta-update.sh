#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# pipeline-meta-update — daemon-aware wrapper (PR 57).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse positional field + value
#   3. POST /v1/pipeline/meta-update?field=<f>&value=<v>
#   4. On 200, pretty-print the data payload
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
FIELD=""
VALUE=""
declare -a PASSTHROUGH=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -*)
            PASSTHROUGH+=("$1"); shift;;
        *)
            if [ -z "$FIELD" ]; then
                FIELD="$1"
            elif [ -z "$VALUE" ]; then
                VALUE="$1"
            fi
            PASSTHROUGH+=("$1"); shift;;
    esac
done

# Need both field and value.
if [ -z "$FIELD" ] || [ -z "$VALUE" ]; then
    echo "Error: both field and value are required." >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="field=$(rt_url_encode "$FIELD")&value=$(rt_url_encode "$VALUE")"

rc=0
RESPONSE="$(rt_call POST /v1/pipeline/meta-update \
    --query "$QUERY" \
    )" || rc=$?

case $rc in
    0)
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
data = resp.get('data') or resp
print(json.dumps(data, indent=2, ensure_ascii=False))
"
        exit 0;;
    2)
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/pipeline/meta-update \
                --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
data = resp.get('data') or resp
print(json.dumps(data, indent=2, ensure_ascii=False))
"
                exit 0
            fi
        fi
        rt_no_daemon_error "pipeline-meta-update.sh";;
    *)
        exit $rc;;
esac
