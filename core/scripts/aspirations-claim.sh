#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# aspirations-claim — daemon-aware wrapper (PR 9b).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Positional goal_id + optional agent_name + --cross-lane flag
#   3. POST /v1/aspirations/claim?id=<goal_id>&agent=<name>[&cross_lane=<reason>]
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
AGENT=""
CROSS_LANE=""
declare -a PASSTHROUGH=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cross-lane)
            CROSS_LANE="${2-}"
            PASSTHROUGH+=("$1" "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --source)
            # Accept-and-ignore for convention symmetry (). Many
            # skill digests (aspirations-select Chaining, aspirations-execute
            # Inputs, loop digest Phase 4) instruct callers to pass
            # `--source {world|agent}` to ALL downstream aspirations-*.sh.
            # claim has no per-source semantics — the daemon endpoint derives
            # source from the goal-id — but without this case the value
            # ("world" or "agent") fell through to the `-*` catch-all which
            # shifts only the flag, leaving the value to be parsed as a
            # positional agent_name. Result was a phantom claimed_by=world
            # row that needed three manual repair calls per occurrence.
            # Consume both flag + value here so the convention is honored.
            shift $(( $# >= 2 ? 2 : 1 ));;
        -*)
            PASSTHROUGH+=("$1"); shift;;
        *)
            if [ -z "$GOAL_ID" ]; then
                GOAL_ID="$1"
            elif [ -z "$AGENT" ]; then
                AGENT="$1"
            fi
            PASSTHROUGH+=("$1"); shift;;
    esac
done

# Default agent from env
if [ -z "$AGENT" ] && [ -n "${MIND_AGENT:-}" ]; then
    AGENT="$MIND_AGENT"
fi

if [ -z "$GOAL_ID" ]; then
    echo "Error: goal_id is required." >&2
    exit 1
fi
if [ -z "$AGENT" ]; then
    echo "Error: agent_name is required (positional or via MIND_AGENT)." >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="id=${GOAL_ID}&agent=${AGENT}"
if [ -n "$CROSS_LANE" ]; then
    ENCODED_CL="$(rt_url_encode "$CROSS_LANE")"
    QUERY="${QUERY}&cross_lane=${ENCODED_CL}"
fi

rc=0
RESPONSE="$(rt_call POST /v1/aspirations/claim --query "$QUERY" 2>&1)" || rc=$?

case $rc in
    0)
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
#  fix: raw_decode tolerates stale-daemon stderr-leakage appended
# after the JSON body (rt_call 2>&1 merges streams). Re-emit residual to
# stderr to preserve daemon-staleness warning visibility.
_src = sys.stdin.read()
resp, _idx = json.JSONDecoder().raw_decode(_src)
_residual = _src[_idx:].strip()
if _residual:
    print(_residual, file=sys.stderr)
goal = resp.get('goal')
if goal is not None:
    print(json.dumps(goal, indent=2, ensure_ascii=False))
"
        exit 0;;
    2)
        # T2.2: parity with CLI cmd_claim exit code. cross_lane_refused -> exit 2.
        if echo "$RESPONSE" | grep -q '"cross_lane_refused"'; then
            printf '%s\n' "$RESPONSE" >&2
            exit 2
        fi
        printf '%s\n' "$RESPONSE" >&2
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback. Try one
        # auto-spawn, then fail loud. See .claude/rules/no-python-cli-fallback.md.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/aspirations/claim --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
#  fix: raw_decode tolerates stale-daemon stderr-leakage appended
# after the JSON body (rt_call 2>&1 merges streams). Re-emit residual to
# stderr to preserve daemon-staleness warning visibility.
_src = sys.stdin.read()
resp, _idx = json.JSONDecoder().raw_decode(_src)
_residual = _src[_idx:].strip()
if _residual:
    print(_residual, file=sys.stderr)
goal = resp.get('goal')
if goal is not None:
    print(json.dumps(goal, indent=2, ensure_ascii=False))
"
                exit 0
            fi
        fi
        rt_no_daemon_error "aspirations-claim.sh";;
    *)
        exit $rc;;
esac
