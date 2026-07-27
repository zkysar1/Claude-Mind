#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# aspirations-release — daemon-aware wrapper (PR 9b).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Positional goal_id
#   3. POST /v1/aspirations/release?id=<goal_id>&source=world
#   4. On 200, print goal JSON to stdout
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# Normalize --goal/--goal-id flag aliases → positional goal id (rewrites $@).
# SSOT for the dual-accept goal-id contract; verify-learning enforces that this
# wrapper sources the normalizer (12-wrapper coverage grep). Restored 2026-05-29
# — dropped by a prior daemon cutover, which silently broke dual-accept and the
# verify-learning normalizer-coverage check.
GOAL_NORMALIZE_TARGET=positional source "$CORE_ROOT/scripts/_goal-arg-normalize.sh"

# --- Parse args -----------------------------------------------------------
GOAL_ID=""
declare -a PASSTHROUGH=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -*)
            PASSTHROUGH+=("$1"); shift;;
        *)
            [ -z "$GOAL_ID" ] && GOAL_ID="$1"
            PASSTHROUGH+=("$1"); shift;;
    esac
done

if [ -z "$GOAL_ID" ]; then
    echo "Error: goal_id is required." >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="id=${GOAL_ID}&source=world"
# Session identity ( outcome 5). The daemon warns when a release is
# invoked by a session that does NOT hold the claim — but it can only do that
# if the caller SAYS which session it is. Without this the guard is structurally
# dead, which is the ORIGINAL bug's shape (claims collided precisely because
# nothing session-scoped was ever transmitted). Best-effort: an empty value is
# omitted and the endpoint behaves exactly as before. MIND_SID is injected into
# every Bash call by bash-agent-inject.py.
if [ -n "${MIND_SID:-}" ]; then
    QUERY="${QUERY}&sid=$(rt_url_encode "$MIND_SID")"
fi

rc=0
RESPONSE="$(rt_call POST /v1/aspirations/release --query "$QUERY")" || rc=$?

case $rc in
    0)
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
# : surface daemon warnings (e.g. a non-holder release) to stderr.
# Without this the endpoint-side guard is invisible to the caller — the
# warning would be computed, returned, and silently dropped here. Mirrors
# aspirations-complete-by.sh, which already forwards warnings this way.
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
goal = resp.get('goal')
if goal is not None:
    print(json.dumps(goal, indent=2, ensure_ascii=False))
"
        exit 0;;
    2)
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/aspirations/release --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
# : surface daemon warnings (e.g. a non-holder release) to stderr.
# Without this the endpoint-side guard is invisible to the caller — the
# warning would be computed, returned, and silently dropped here. Mirrors
# aspirations-complete-by.sh, which already forwards warnings this way.
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
goal = resp.get('goal')
if goal is not None:
    print(json.dumps(goal, indent=2, ensure_ascii=False))
"
                exit 0
            fi
        fi
        rt_no_daemon_error "aspirations-release.sh";;
    *)
        exit $rc;;
esac
