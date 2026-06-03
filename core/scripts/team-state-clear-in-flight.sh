#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Remove the in_flight block from an agent's status in the shared team state.
# Daemon path: POST /v1/team-state/clear-in-flight?agent=<name>
# Usage: bash core/scripts/team-state-clear-in-flight.sh --agent <name>
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
AGENT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent)  AGENT="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --author) shift $(( $# >= 2 ? 2 : 1 ));;  # consumed by daemon via X-Mind-Agent header
        *) shift;;
    esac
done

if [ -z "$AGENT" ]; then
    echo "Error: --agent is required" >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="agent=$(rt_url_encode "$AGENT")"

_translate() {
    # Reproduce CLI stdout from daemon JSON response.
    # CLI prints: "in_flight cleared for <agent>" or "in_flight already absent for <agent>"
    # Daemon returns: {"ok": true, "agent": "<agent>", "cleared": true/false}
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
agent = resp.get('agent', '')
if resp.get('cleared'):
    print(f'in_flight cleared for {agent}')
else:
    print(f'in_flight already absent for {agent}')
"
}

rc=0
RESPONSE="$(rt_call POST /v1/team-state/clear-in-flight --query "$QUERY")" || rc=$?

case $rc in
    0) _translate "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/team-state/clear-in-flight --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then _translate "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "team-state-clear-in-flight.sh";;
    *) exit $rc;;
esac
