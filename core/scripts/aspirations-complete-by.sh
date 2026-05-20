#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# aspirations-complete-by — daemon-aware wrapper (PR 9c).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse --source / --key-finding / positionals
#   3. POST /v1/aspirations/complete-by with params as query string
#   4. On 200, print goal JSON to stdout (matches legacy CLI shape)
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
SOURCE_VAL="world"
KEY_FINDING=""
declare -a POSITIONAL=()
declare -a PASSTHROUGH_SOURCE=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            SOURCE_VAL="${2-}"
            PASSTHROUGH_SOURCE=(--source "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --key-finding)
            KEY_FINDING="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        *)
            POSITIONAL+=("$1"); shift;;
    esac
done
set -- ${POSITIONAL[@]+"${POSITIONAL[@]}"}

GOAL_ID="${1:-}"
AGENT_VAL="${2:-${MIND_AGENT:-}}"

# Missing goal_id -> error
if [ -z "$GOAL_ID" ]; then
    echo "Error: goal_id is required." >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="goal_id=$(rt_url_encode "$GOAL_ID")&source=$(rt_url_encode "$SOURCE_VAL")"
[ -n "$AGENT_VAL" ] && QUERY="${QUERY}&agent_name=$(rt_url_encode "$AGENT_VAL")"
[ -n "$KEY_FINDING" ] && QUERY="${QUERY}&key_finding=$(rt_url_encode "$KEY_FINDING")"

rc=0
RESPONSE="$(rt_call POST /v1/aspirations/complete-by \
    --query "$QUERY")" || rc=$?

case $rc in
    0)
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
goal = resp.get('goal')
if goal is None:
    print(json.dumps(resp, indent=2, ensure_ascii=False))
else:
    print(json.dumps(goal, indent=2, ensure_ascii=False))
"
        exit 0;;
    2)
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/aspirations/complete-by \
                --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
goal = resp.get('goal')
if goal is None:
    print(json.dumps(resp, indent=2, ensure_ascii=False))
else:
    print(json.dumps(goal, indent=2, ensure_ascii=False))
"
                exit 0
            fi
        fi
        rt_no_daemon_error "aspirations-complete-by.sh";;
    *)
        exit $rc;;
esac
