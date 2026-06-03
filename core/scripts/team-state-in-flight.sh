#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Mark an agent as in-flight on a goal in the shared team state.
# Daemon path: rt_call POST /v1/team-state/in-flight (query params).
# Usage: bash core/scripts/team-state-in-flight.sh --agent <name> --goal-id <id> --title <text> --phase <n>
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# Value-arg pattern: "${2-}" + safe shift; see _runtime.sh / tree-read.sh.
AGENT=""; GOAL_ID=""; TITLE=""; PHASE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent)   AGENT="${2-}";   shift $(( $# >= 2 ? 2 : 1 ));;
        --goal-id) GOAL_ID="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --title)   TITLE="${2-}";   shift $(( $# >= 2 ? 2 : 1 ));;
        --phase)   PHASE="${2-}";   shift $(( $# >= 2 ? 2 : 1 ));;
        --author)  shift $(( $# >= 2 ? 2 : 1 ));;  # handled by X-Mind-Agent header
        *) shift;;
    esac
done

# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY=""
_append_q() { [ -n "$QUERY" ] && QUERY+="&"; QUERY+="$1"; }
[ -n "$AGENT" ]   && _append_q "agent=$(rt_url_encode "$AGENT")"
[ -n "$GOAL_ID" ] && _append_q "goal_id=$(rt_url_encode "$GOAL_ID")"
[ -n "$TITLE" ]   && _append_q "title=$(rt_url_encode "$TITLE")"
[ -n "$PHASE" ]   && _append_q "phase=$(rt_url_encode "$PHASE")"

_translate() {
    # Reproduce CLI stdout: "in_flight set for {agent}: {goal_id} phase={phase}"
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
print(f\"in_flight set for {resp['agent']}: {resp['goal_id']} phase={resp['phase']}\")
"
}

rc=0
RESPONSE="$(rt_call POST /v1/team-state/in-flight --query "$QUERY")" || rc=$?

case $rc in
    0) _translate "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/team-state/in-flight --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then _translate "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "team-state-in-flight.sh";;
    *) exit $rc;;
esac
