#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# aspirations-complete — daemon-aware wrapper (PR 9a).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse args + read stdin body (if --intent-satisfied)
#   3. POST /v1/aspirations/complete with params mapped to query string
#   4. On 200, re-emit `warnings[]` to stderr and print `aspiration` to stdout
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
SOURCE_VAL="world"
ASP_ID=""
FORCE=0
INTENT_SATISFIED=0
declare -a PASSTHROUGH=()
declare -a PASSTHROUGH_SOURCE=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            SOURCE_VAL="${2-}"
            PASSTHROUGH_SOURCE=(--source "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --force)
            FORCE=1
            PASSTHROUGH+=("$1"); shift;;
        --intent-satisfied)
            INTENT_SATISFIED=1
            PASSTHROUGH+=("$1"); shift;;
        -*)
            # Unknown flag — passthrough for argparse on fallback
            PASSTHROUGH+=("$1"); shift;;
        *)
            # Positional asp_id (first non-flag wins)
            [ -z "$ASP_ID" ] && ASP_ID="$1"
            PASSTHROUGH+=("$1"); shift;;
    esac
done

# Missing asp_id → error
if [ -z "$ASP_ID" ]; then
    echo "Error: asp_id is required." >&2
    exit 1
fi

# Read stdin BEFORE invoking the daemon. If --intent-satisfied, the body is
# the intent_satisfaction JSON block; otherwise body is empty.
BODY=""
if [ "$INTENT_SATISFIED" = "1" ]; then
    BODY="$(cat)"
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="asp_id=${ASP_ID}&source=${SOURCE_VAL}"
[ "$FORCE" = "1" ] && QUERY="${QUERY}&force=true"
[ "$INTENT_SATISFIED" = "1" ] && QUERY="${QUERY}&intent_satisfied=true"

rc=0
if [ -n "$BODY" ]; then
    RESPONSE="$(rt_call POST /v1/aspirations/complete \
        --query "$QUERY" \
        --body-string "$BODY")" || rc=$?
else
    RESPONSE="$(rt_call POST /v1/aspirations/complete \
        --query "$QUERY")" || rc=$?
fi

case $rc in
    0)
        # 200: parse response. Re-emit warnings[] to stderr and print the
        # aspiration record to stdout (matches legacy CLI json.dumps shape).
        #  fix: route response via stdin (was argv). Windows argv
        # limit is ~32KB; large archived aspirations ( had 23 goals
        # ~57KB serialized) hit "Argument list too long" at exec time. The
        # daemon-side archival had already succeeded but the wrapper exited
        # non-zero from this print failure, leaving callers thinking the
        # operation failed. stdin path has no length limit. The 17 sibling
        # daemon wrappers share this shape; tracked as a follow-up Idea.
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
asp = resp.get('aspiration')
if asp is None:
    print(json.dumps({k: v for k, v in resp.items() if k != 'warnings'},
                     indent=2, ensure_ascii=False))
else:
    print(json.dumps(asp, indent=2, ensure_ascii=False))
"
        exit 0;;
    2)
        # Daemon answered 4xx/5xx; body already written to stderr by rt_curl.
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            if [ -n "$BODY" ]; then
                RESPONSE="$(rt_call POST /v1/aspirations/complete \
                    --query "$QUERY" \
                    --body-string "$BODY")" || rc=$?
            else
                RESPONSE="$(rt_call POST /v1/aspirations/complete \
                    --query "$QUERY")" || rc=$?
            fi
            if [ "$rc" = "0" ]; then
                #  fix: stdin route (same rationale as success path above).
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
asp = resp.get('aspiration')
if asp is None:
    print(json.dumps({k: v for k, v in resp.items() if k != 'warnings'},
                     indent=2, ensure_ascii=False))
else:
    print(json.dumps(asp, indent=2, ensure_ascii=False))
"
                exit 0
            fi
        fi
        rt_no_daemon_error "aspirations-complete.sh";;
    *)
        exit $rc;;
esac
